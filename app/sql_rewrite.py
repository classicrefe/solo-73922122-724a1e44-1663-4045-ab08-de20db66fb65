"""把校验通过的 SELECT 改写为携带溯源信息的等价查询。

改写思路（AST 级，基于 sqlglot）：
1. 展开 SELECT * / t.*，限定所有列引用为「别名.列」，并做未知列/歧义列检查；
2. 物理表替换为快照临时表，并在每行附加一个 jsonb 溯源状态
   ``{"表名": {"行ID": 出现次数}}``；
3. INNER JOIN 用深合并函数 prov_add 合并两侧溯源；LEFT JOIN 对右表未匹配
   （补空）的行只保留左表溯源 —— 补空不算来源；
4. 分组聚合时，组内溯源用自定义聚合 prov_agg 按 (表, 行ID) 累加出现次数；
5. CTE 被内联成派生表，且每次引用都使用独立深拷贝，因此 CTE 在外层每被
   使用一次，其来源就独立累加一次。

原查询的输出列、值与重数完全不变，只额外追加一个保留列 ``__prov__``。
"""
from __future__ import annotations

import copy

from sqlglot import exp

from .sql_validate import PROV_COLUMN, TABLE_SCHEMA, UnsupportedSQLError

# 会话级聚合 / 深合并函数（创建在 pg_temp 中，调用需显式限定）
PROV_AGG = "prov_agg"
PROV_AGG_STATE = "prov_agg_state"
PROV_ADD = "prov_add"
_PG_TEMP = "pg_temp"


def _prov_func(name: str, *args: exp.Expression) -> exp.Anonymous:
    # 显式 pg_temp. 限定：Postgres 函数名解析不会自动包含临时模式
    return exp.Anonymous(
        this=exp.Dot(
            this=exp.to_identifier(_PG_TEMP),
            expression=exp.to_identifier(name),
        ),
        expressions=list(args),
    )


# --------------------------------------------------------------------------- #
# 语义解析：源、列限定、星号展开、GROUP BY 序号
# --------------------------------------------------------------------------- #
class Source:
    """FROM/JOIN 链上的一个数据源。"""

    def __init__(self, alias: str, table_name: str, node: exp.Table):
        self.alias = alias
        self.table_name = table_name
        self.node = node

    @property
    def is_physical(self) -> bool:
        return self.table_name in TABLE_SCHEMA

    def output_columns(self, cte_outputs: dict[str, list[str]]) -> list[str]:
        if self.is_physical:
            return list(TABLE_SCHEMA[self.table_name])
        return list(cte_outputs[self.table_name])


def _sources_of(select: exp.Select) -> list[Source]:
    sources: list[Source] = []
    first = select.args["from_"].this
    if not isinstance(first, exp.Table):
        raise UnsupportedSQLError("FROM 项必须是表或 CTE")
    sources.append(Source(first.alias or first.name, first.name, first))
    # JOIN 只取 FROM 直接挂载的，不递归进 WITH/CTE 子树
    with_ = select.args.get("with_")
    cte_joins = set(with_.find_all(exp.Join)) if with_ is not None else set()
    for join in select.find_all(exp.Join):
        if join in cte_joins:
            continue
        table = join.this
        if not isinstance(table, exp.Table):
            raise UnsupportedSQLError("JOIN 项必须是表或 CTE")
        sources.append(Source(table.alias or table.name, table.name, table))
    return sources


def _expand_stars(select: exp.Select, cte_outputs: dict[str, list[str]]) -> None:
    sources = _sources_of(select)
    by_alias = {s.alias: s for s in sources}
    new_projections: list[exp.Expression] = []
    for proj in select.expressions:
        # COUNT(*) / COUNT(1) 里的星号不是投影星号，跳过
        star = proj.find(exp.Star)
        if star is not None and proj.find(exp.AggFunc) is not None:
            star = None
        if star is None:
            new_projections.append(proj)
            continue
        # 注意 exp.Star 是 exp.Column 的子类，必须先判裸 Star
        if isinstance(proj, exp.Star):
            for src in sources:
                for col in src.output_columns(cte_outputs):
                    new_projections.append(exp.column(col, table=src.alias))
        elif isinstance(proj, exp.Column) and isinstance(proj.this, exp.Star):
            target = by_alias.get(proj.table)
            if target is None:
                raise UnsupportedSQLError(f"星号引用了未知表别名 {proj.table}")
            for col in target.output_columns(cte_outputs):
                new_projections.append(exp.column(col, table=target.alias))
        else:
            raise UnsupportedSQLError("不支持的星号表达式")
    select.set("expressions", new_projections)


def _resolve_group_ordinals(select: exp.Select) -> None:
    group = select.args.get("group")
    if group is None:
        return
    resolved: list[exp.Expression] = []
    n_proj = len(select.expressions)
    for gexpr in group.expressions:
        if isinstance(gexpr, exp.Literal) and gexpr.is_number:
            ordinal = int(gexpr.name)
            if not 1 <= ordinal <= n_proj:
                raise UnsupportedSQLError(
                    f"GROUP BY 序号 {ordinal} 超出 SELECT 输出列范围（共 {n_proj} 列）"
                )
            target = select.expressions[ordinal - 1].copy()
            if isinstance(target, exp.Alias):
                target = target.this
            resolved.append(target)
        else:
            resolved.append(gexpr)
    group.set("expressions", resolved)


def _qualify_columns(
    select: exp.Select, cte_outputs: dict[str, list[str]]
) -> None:
    sources = _sources_of(select)
    visible: dict[str, list[str]] = {}
    for src in sources:
        visible[src.alias] = src.output_columns(cte_outputs)
    aliases = list(visible)

    # 只限定本 SELECT 作用域内的列，排除 WITH/CTE 子树（CTE 主体单独处理）
    with_ = select.args.get("with_")
    cte_columns = set(with_.find_all(exp.Column)) if with_ is not None else set()

    for col in select.find_all(exp.Column):
        if col in cte_columns:
            continue
        if isinstance(col.this, exp.Star):
            continue
        if col.table:
            if col.table not in visible:
                raise UnsupportedSQLError(f"未知表别名 {col.table}")
            if col.name not in visible[col.table]:
                raise UnsupportedSQLError(f"列 {col.table}.{col.name} 不存在")
            continue
        owners = [a for a in aliases if col.name in visible[a]]
        if not owners:
            raise UnsupportedSQLError(f"列 {col.name} 不存在于任何 FROM 表中")
        if len(owners) > 1:
            raise UnsupportedSQLError(
                f"列 {col.name} 在多个表中存在（{', '.join(owners)}），必须用别名限定"
            )
        col.set("table", exp.to_identifier(owners[0]))


def prepare_select(
    select: exp.Select, cte_outputs: dict[str, list[str]]
) -> None:
    """星号展开、GROUP BY 序号解析、列限定（原地修改）。"""
    _expand_stars(select, cte_outputs)
    _resolve_group_ordinals(select)
    _qualify_columns(select, cte_outputs)


# --------------------------------------------------------------------------- #
# 溯源表达式构造
# --------------------------------------------------------------------------- #
def _base_prov(table_alias: str, table_name: str) -> exp.Anonymous:
    """物理表行的初始溯源：jsonb_build_object('表', jsonb_build_object(id::text, 1))。"""
    return exp.Anonymous(
        this="jsonb_build_object",
        expressions=[
            exp.Literal.string(table_name),
            exp.Anonymous(
                this="jsonb_build_object",
                expressions=[
                    exp.cast(exp.column("id", table=table_alias), "text"),
                    exp.Literal.number(1),
                ],
            ),
        ],
    )


def _prov_col(table_alias: str) -> exp.Column:
    return exp.column(PROV_COLUMN, table=table_alias)


# --------------------------------------------------------------------------- #
# 改写
# --------------------------------------------------------------------------- #
def _collect_cte_outputs(root: exp.Select) -> dict[str, list[str]]:
    outputs: dict[str, list[str]] = {}
    with_ = root.args.get("with_")
    if with_ is None:
        return outputs
    for cte in with_.expressions:
        names: list[str] = []
        for i, proj in enumerate(cte.this.expressions):
            if isinstance(proj, exp.Alias):
                names.append(proj.alias)
            elif isinstance(proj, exp.Column):
                names.append(proj.name)
            else:
                names.append(f"col_{i + 1}")
        outputs[cte.alias] = names
    return outputs


def prepare(root: exp.Select) -> tuple[exp.Select, dict[str, list[str]]]:
    """校验后预处理：展开星号、限定列。返回 (新AST, CTE输出列映射)。"""
    ast = root.copy()
    cte_outputs = _collect_cte_outputs(ast)
    with_ = ast.args.get("with_")
    if with_ is not None:
        for cte in with_.expressions:
            prepare_select(cte.this, cte_outputs)
    prepare_select(ast, cte_outputs)
    return ast, cte_outputs


def rewrite(
    root: exp.Select, snapshot_map: dict[str, str]
) -> exp.Select:
    """生成携带 __prov__ 溯源列的查询 AST。"""
    ast, _ = prepare(root)
    cte_bodies: dict[str, exp.Select] = {}
    with_ = ast.args.get("with_")
    if with_ is not None:
        for cte in with_.expressions:
            cte_bodies[cte.alias] = cte.this
        ast.set("with_", None)
    # 最外层用带 finalfunc 的聚合，输出可直接返回的溯源数组
    _rewrite_select(ast, cte_bodies, snapshot_map, finalize=True)
    return ast


def _rewrite_select(
    select: exp.Select,
    cte_bodies: dict[str, exp.Select],
    snapshot_map: dict[str, str],
    *,
    finalize: bool,
) -> None:
    # 先收集本层 FROM/JOIN 链（替换后再 find_all 会递归进派生表内部）
    from_ = select.args["from_"]
    first_table: exp.Table = from_.this
    join_specs = [(join, join.this) for join in select.find_all(exp.Join)]

    # 1) 替换 FROM 源（物理表包快照子查询；CTE 递归内联，内层保持状态对象）
    from_.set(
        "this",
        _rewrite_source(first_table, cte_bodies, snapshot_map),
    )
    for join, table in join_specs:
        join.set("this", _rewrite_source(table, cte_bodies, snapshot_map))

    # 2) 计算行级溯源表达式（沿 JOIN 链合并，LEFT 补空不引入右源）
    first_alias = first_table.alias_or_name
    row_prov: exp.Expression = _prov_col(first_alias)
    for join, _ in join_specs:
        right_alias = join.this.alias_or_name
        right_p = _prov_col(right_alias)
        if (join.side or "").upper() == "LEFT":
            row_prov = exp.Case(
                ifs=[exp.If(this=right_p.is_(exp.Null()), true=row_prov)],
                default=_prov_func(PROV_ADD, row_prov, right_p),
            )
        else:
            row_prov = _prov_func(PROV_ADD, row_prov, right_p)

    # 3) 追加溯源投影
    #    - 内层派生表/CTE：用 state 聚合，输出仍是 jsonb 对象，可继续向外合并
    #    - 最外层：用 final 聚合，输出 [{t,id,n}, ...] 溯源数组
    #    聚合判定必须只看本层投影/HAVING 中的聚合函数，不能递归进派生表
    own_aggregates = {
        agg
        for proj in select.expressions
        for agg in proj.find_all(exp.AggFunc)
    }
    having = select.args.get("having")
    if having is not None:
        own_aggregates.update(having.find_all(exp.AggFunc))
    is_aggregate = bool(own_aggregates)
    if is_aggregate:
        agg_name = PROV_AGG if finalize else PROV_AGG_STATE
        prov_expr: exp.Expression = _prov_func(agg_name, row_prov)
    elif finalize:
        # 最外层非聚合：把行级状态对象转成标准数组格式
        prov_expr = _prov_func(f"{PROV_AGG}_final", row_prov)
    else:
        prov_expr = row_prov
    select.expressions.append(exp.alias_(prov_expr, PROV_COLUMN))


def _rewrite_source(
    table: exp.Table,
    cte_bodies: dict[str, exp.Select],
    snapshot_map: dict[str, str],
) -> exp.Subquery:
    alias = table.alias_or_name
    if table.name in cte_bodies:
        # 每次引用独立深拷贝，保证 CTE 重复使用时来源各自累加
        inner = copy.deepcopy(cte_bodies[table.name])
        _rewrite_select(
            inner, cte_bodies, snapshot_map, finalize=False
        )
        return exp.Subquery(this=inner, alias=exp.to_identifier(alias))

    snapshot_name = snapshot_map[table.name]
    snap_table = exp.table_(snapshot_name, alias=exp.to_identifier(alias))
    sub = exp.Select()
    sub.set("expressions", [exp.Star()])
    sub.expressions.append(
        exp.alias_(_base_prov(alias, table.name), PROV_COLUMN)
    )
    sub.set("from_", exp.From(this=snap_table))
    return exp.Subquery(this=sub, alias=exp.to_identifier(alias))


def render_sql(select: exp.Select) -> str:
    return select.sql(dialect="postgres", pretty=True)
