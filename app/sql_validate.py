"""SQL 白名单校验。

只接受题目规定的语法：
- 单条 SELECT，可选一个非递归 CTE（CTE 必须是先聚合的查询）
- INNER / LEFT 等值连接（ON 全部为等值合取）
- WHERE / GROUP BY / HAVING
- 聚合仅 COUNT / SUM（拒绝 DISTINCT、窗口）
- 表达式仅允许列、字面量、比较/逻辑/算术/IN/BETWEEN/IS NULL 等
- 只允许访问预置表 samples(id, batch) 与 results(id, sample_id, value)

任何其他语法（子查询、集合操作、窗口、DML、DDL、多语句、ORDER BY/LIMIT、
函数调用、DISTINCT 等）均抛出 UnsupportedSQLError 明确拒绝。
"""
from __future__ import annotations

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError, TokenError

# 物理表结构（列 -> 通用类型）
TABLE_SCHEMA: dict[str, dict[str, str]] = {
    "samples": {"id": "INTEGER", "batch": "VARCHAR"},
    "results": {"id": "INTEGER", "sample_id": "INTEGER", "value": "NUMERIC"},
}

# 溯源保留列名，用户查询不允许声明同名输出
PROV_COLUMN = "__prov__"

# 允许出现的 AST 节点白名单（按类精确匹配，sqlglot 子类众多，逐一列出）
_ALLOWED_NODES: frozenset[type[exp.Expression]] = frozenset(
    {
        # 结构
        exp.Select,
        exp.From,
        exp.Join,
        exp.Table,
        exp.TableAlias,
        exp.Alias,
        exp.Column,
        exp.Identifier,
        exp.Star,
        exp.Where,
        exp.Group,
        exp.Having,
        # 字面量
        exp.Literal,
        exp.Boolean,
        exp.Null,
        # 比较 / 逻辑 / 谓词
        exp.EQ,
        exp.NEQ,
        exp.GT,
        exp.GTE,
        exp.LT,
        exp.LTE,
        exp.And,
        exp.Or,
        exp.Not,
        exp.Is,
        exp.In,
        exp.Interval,
        exp.Between,
        exp.Like,
        # 算术
        exp.Add,
        exp.Sub,
        exp.Mul,
        exp.Div,
        exp.Neg,
        exp.Paren,
        # 条件
        exp.Case,
        exp.If,
        # 聚合（结构白名单；种类在结构检查中再限定）
        exp.Count,
        exp.Sum,
        # CTE
        exp.With,
        exp.CTE,
    }
)

# 允许的二元谓词中，连接 ON 只接受等值
_JOIN_EQ_NODES = (exp.EQ,)


class UnsupportedSQLError(ValueError):
    """SQL 不在受支持白名单内。"""


def parse_select(sql: str) -> exp.Select:
    """解析并确认是单条 SELECT 语句。"""
    if not sql or not sql.strip():
        raise UnsupportedSQLError("SQL 内容为空")
    try:
        statements = sqlglot.parse(sql.strip(), read="postgres")
    except (ParseError, TokenError) as exc:
        raise UnsupportedSQLError(f"SQL 解析失败：{exc}") from exc
    statements = [s for s in statements if s is not None]
    if len(statements) != 1:
        raise UnsupportedSQLError("只允许提交单条 SELECT 语句，拒绝多语句/堆叠查询")
    stmt = statements[0]
    if not isinstance(stmt, exp.Select):
        raise UnsupportedSQLError(
            f"只允许 SELECT 查询，检测到 {type(stmt).__name__.upper()} 语句"
        )
    return stmt


def _check_node_whitelist(select: exp.Select) -> None:
    """遍历整棵 AST，任何不在白名单的节点都明确拒绝。"""
    for node in select.walk():
        node_type = type(node)
        if node_type in _ALLOWED_NODES:
            continue
        # 给出对分析员友好的拒绝原因
        name = node_type.__name__
        detail = ""
        if isinstance(node, exp.Window):
            detail = "窗口函数"
        elif isinstance(node, exp.Subquery):
            detail = "子查询（仅允许一个 CTE，不允许普通子查询）"
        elif isinstance(node, (exp.Union, exp.Except, exp.Intersect)):
            detail = "集合操作（UNION/INTERSECT/EXCEPT）"
        elif isinstance(node, exp.Func):
            detail = f"函数调用 {node.sql()[:40]}"
        elif isinstance(node, exp.Distinct):
            detail = "DISTINCT"
        elif isinstance(node, (exp.Insert, exp.Update, exp.Delete)):
            detail = "写操作"
        raise UnsupportedSQLError(f"不支持的语法：{detail or name}，已明确拒绝")


def _check_select_shape(select: exp.Select, *, is_cte_body: bool = False) -> None:
    """检查单个 SELECT 的子句形状。"""
    if select.args.get("distinct") is not None:
        raise UnsupportedSQLError("不支持 SELECT DISTINCT")
    for key, label in (
        ("order", "ORDER BY"),
        ("limit", "LIMIT"),
        ("offset", "OFFSET"),
        ("union", "集合操作"),
        ("qualify", "QUALIFY"),
        ("window", "WINDOW 子句"),
        ("lateral", "LATERAL"),
        ("pivot", "PIVOT"),
        ("top", "TOP"),
    ):
        if select.args.get(key) is not None:
            raise UnsupportedSQLError(f"不支持 {label} 子句")
    if select.args.get("for_") is not None or select.args.get("lock") is not None:
        raise UnsupportedSQLError("不支持 FOR UPDATE / LOCK 子句")
    if not select.args.get("from_"):
        raise UnsupportedSQLError("查询必须包含 FROM 子句")
    # 不支持 BETWEEN SYMMETRIC（仅支持普通 BETWEEN）
    for between in select.find_all(exp.Between):
        if between.args.get("symmetric"):
            raise UnsupportedSQLError("不支持 BETWEEN SYMMETRIC，请使用普通 BETWEEN")
    # 聚合白名单：白名单节点已保证只有 Count/Sum，这里再拦截 distinct 变体
    for agg in select.find_all(exp.AggFunc):
        if not isinstance(agg, (exp.Count, exp.Sum)):
            raise UnsupportedSQLError(
                f"仅支持 COUNT/SUM 聚合，检测到 {type(agg).__name__.upper()}"
            )
        if agg.find(exp.Distinct) is not None:
            raise UnsupportedSQLError("不支持 COUNT(DISTINCT ...)/SUM(DISTINCT ...)")
        # 聚合不允许嵌套（白名单下实际很难出现，双保险）
        for inner in agg.find_all(exp.AggFunc):
            if inner is not agg:
                raise UnsupportedSQLError("不允许嵌套聚合函数")
    # WHERE 里不允许聚合
    where = select.args.get("where")
    if where is not None and where.find(exp.AggFunc) is not None:
        raise UnsupportedSQLError("WHERE 子句中不允许使用聚合函数")
    # 输出列名不允许占用保留列
    for proj in select.expressions:
        if isinstance(proj, exp.Alias) and proj.alias.lower() == PROV_COLUMN:
            raise UnsupportedSQLError(f"输出列名 {PROV_COLUMN} 为系统保留名")
    if is_cte_body and not select.args.get("group"):
        raise UnsupportedSQLError("CTE 必须先聚合：CTE 主体必须包含 GROUP BY")


def _check_joins(select: exp.Select) -> None:
    """仅允许 INNER / LEFT，且 ON 为等值合取。"""
    from_ = select.args["from_"]
    if from_.args.get("pivots"):
        raise UnsupportedSQLError("不支持 PIVOT")
    for join in select.find_all(exp.Join):
        if join.args.get("using") is not None:
            raise UnsupportedSQLError("不支持 JOIN ... USING，请改写为 ON 等值条件")
        kind = (join.kind or "").upper()
        side = (join.side or "").upper()
        if kind == "CROSS" or (not kind and not join.args.get("on")):
            raise UnsupportedSQLError("不支持 CROSS JOIN 或逗号隐式连接")
        if kind not in ("INNER", "") or side not in ("", "LEFT"):
            raise UnsupportedSQLError(
                f"仅支持 INNER JOIN 与 LEFT JOIN，检测到 {side + ' ' + kind}".strip()
            )
        on = join.args.get("on")
        if on is None:
            raise UnsupportedSQLError("JOIN 必须带 ON 等值条件")
        for conjunct in _flatten_and(on):
            if not isinstance(conjunct, _JOIN_EQ_NODES):
                raise UnsupportedSQLError(
                    "JOIN ON 只允许列与列的等值条件（AND 连接），"
                    f"检测到 {conjunct.sql()[:60]}"
                )
            left, right = conjunct.this, conjunct.expression
            if not (isinstance(left, exp.Column) and isinstance(right, exp.Column)):
                raise UnsupportedSQLError("JOIN ON 等值条件两侧必须都是列引用")


def _flatten_and(node: exp.Expression) -> list[exp.Expression]:
    if isinstance(node, exp.And):
        return _flatten_and(node.this) + _flatten_and(node.expression)
    return [node]


def _check_cte(select: exp.Select) -> exp.CTE | None:
    """最多一个、非递归、主体先聚合的 CTE。"""
    with_ = select.args.get("with_")
    if with_ is None:
        return None
    if with_.args.get("recursive"):
        raise UnsupportedSQLError("不支持 RECURSIVE CTE")
    ctes = list(with_.expressions)
    if len(ctes) > 1:
        raise UnsupportedSQLError("最多允许一个 CTE")
    cte = ctes[0]
    body = cte.this
    if not isinstance(body, exp.Select):
        raise UnsupportedSQLError("CTE 主体必须是 SELECT")
    # CTE 主体内不允许再带 WITH/子查询（白名单节点已拦 Subquery，这里显式说明）
    if body.args.get("with_") is not None:
        raise UnsupportedSQLError("CTE 内不允许再嵌套 CTE")
    _check_select_shape(body, is_cte_body=True)
    _check_joins(body)
    # CTE 主体引用的必须是物理表
    for table in body.find_all(exp.Table):
        if table.name not in TABLE_SCHEMA:
            raise UnsupportedSQLError(
                f"CTE 内只能访问预置表 samples/results，检测到 {table.name}"
            )
    # 外层不允许再出现物理表之外的表名
    outer_tables = [
        t
        for t in select.find_all(exp.Table)
        if t is not None and t.name not in TABLE_SCHEMA
    ]
    cte_name = cte.alias
    for table in outer_tables:
        if table.name != cte_name:
            raise UnsupportedSQLError(
                f"表 {table.name} 不存在；仅允许 samples、results"
                + (f" 与 CTE {cte_name}" if cte_name else "")
            )
    return cte


def _check_physical_tables(select: exp.Select) -> None:
    for table in select.find_all(exp.Table):
        if table.name not in TABLE_SCHEMA and not _is_cte_ref(table, select):
            raise UnsupportedSQLError(
                f"表 {table.name} 不存在；仅允许访问 samples 与 results"
            )
        if table.args.get("pivots"):
            raise UnsupportedSQLError("不支持表函数/PIVOT")


def _is_cte_ref(table: exp.Table, root: exp.Select) -> bool:
    with_ = root.args.get("with_")
    if with_ is None:
        return False
    return table.name in {c.alias for c in with_.expressions}


def _check_no_duplicate_aliases(select: exp.Select) -> None:
    """同一 SELECT 的 FROM/JOIN 链上别名不可重复（CTE 内外是不同作用域）。"""
    seen: set[str] = set()
    tables = [select.args["from_"].this]
    tables.extend(j.this for j in select.find_all(exp.Join))
    for table in tables:
        if not isinstance(table, exp.Table):
            continue
        alias = table.alias or table.name
        if alias in seen:
            raise UnsupportedSQLError(f"表别名 {alias} 重复")
        seen.add(alias)


def _cte_output_names(root: exp.Select) -> dict[str, list[str]]:
    """CTE 名 -> 其输出列名（用于外层引用校验）。"""
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


def _visible_columns(
    select: exp.Select, cte_outputs: dict[str, list[str]]
) -> dict[str, list[str]]:
    visible: dict[str, list[str]] = {}
    first = select.args["from_"].this
    chain = [first] + [j.this for j in select.find_all(exp.Join)]
    for table in chain:
        if not isinstance(table, exp.Table):
            continue
        alias = table.alias or table.name
        if table.name in TABLE_SCHEMA:
            visible[alias] = list(TABLE_SCHEMA[table.name])
        else:
            visible[alias] = list(cte_outputs.get(table.name, []))
    return visible


def _check_columns(
    select: exp.Select, cte_outputs: dict[str, list[str]]
) -> None:
    """逐作用域校验列引用：未知别名、未知列、歧义列。"""
    visible = _visible_columns(select, cte_outputs)
    aliases = list(visible)
    with_ = select.args.get("with_")
    cte_cols = set(with_.find_all(exp.Column)) if with_ is not None else set()

    for col in select.find_all(exp.Column):
        if col in cte_cols or isinstance(col.this, exp.Star):
            continue
        if col.table:
            if col.table not in visible:
                raise UnsupportedSQLError(f"未知表别名 {col.table}")
            if col.name not in visible[col.table]:
                raise UnsupportedSQLError(
                    f"列 {col.table}.{col.name} 不存在"
                )
            continue
        owners = [a for a in aliases if col.name in visible[a]]
        if not owners:
            raise UnsupportedSQLError(
                f"列 {col.name} 不存在于任何 FROM 表中"
            )
        if len(owners) > 1:
            raise UnsupportedSQLError(
                f"列 {col.name} 在多个表中存在（{', '.join(owners)}），必须用别名限定"
            )


def validate_sql(sql: str) -> exp.Select:
    """完整校验入口，返回可用于改写的合格 AST（仍是原始形态）。"""
    select = parse_select(sql)
    _check_node_whitelist(select)
    cte = _check_cte(select)
    _check_select_shape(select)
    _check_joins(select)
    _check_physical_tables(select)
    _check_no_duplicate_aliases(select)
    cte_outputs = _cte_output_names(select)
    # CTE 主体作用域
    if cte is not None:
        _check_no_duplicate_aliases(cte.this)
        _check_columns(cte.this, cte_outputs)
    # 外层作用域
    _check_columns(select, cte_outputs)
    return select
