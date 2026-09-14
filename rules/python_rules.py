"""Python 确定性规则库（FR-020、SRS §6.3）。

每条规则都声明规则 ID、CWE、严重级别、置信度基线与自动修复能力，
并给出可追溯证据（命中行的原文）。规则只报告可复现的命中，不因 LLM 意见撤销。
"""

from __future__ import annotations

import ast
import re
from collections.abc import Iterable

from domain.enums import FixCategory, Severity
from rules.base import (
    FileContext,
    Rule,
    RuleHit,
    RuleKind,
    call_name,
    format_source,
    node_line,
    register_check,
    string_value,
)

SQL_KEYWORDS = ("select", "insert", "update", "delete", "replace", "with ")

SECRET_NAME_PATTERN = (
    # 前缀允许下划线（DB_PASSWORD、db_password），但不允许字母/数字紧邻（避免 mysecret 误报）。
    r"(?i)(?<![A-Za-z0-9])(api[_-]?key|apikey|secret|secret[_-]?key|password|passwd|pwd|token|"
    r"access[_-]?key|private[_-]?key|auth[_-]?token|client[_-]?secret)\b\s*[:=]\s*"
    r"([\"'][^\"'\n]{8,}[\"']|[A-Za-z0-9_\-]{16,})"
)

SECRET_PLACEHOLDERS = ("changeme", "example", "placeholder", "your_", "xxx", "todo", "dummy", "test")


def _is_secret_literal(value: str) -> bool:
    lowered = value.lower()
    if len(value) < 8:
        return False
    return not any(token in lowered for token in SECRET_PLACEHOLDERS)


# ---------------------------------------------------------------------------
# 正则规则
# ---------------------------------------------------------------------------


def _regex_hits(ctx: FileContext, rule: Rule) -> Iterable[RuleHit]:
    pattern = rule.compiled()
    for lineno, text in enumerate(ctx.lines, start=1):
        match = pattern.search(text)
        if not match:
            continue
        yield RuleHit(
            line=lineno,
            evidence=text.strip()[:300],
            message=rule.description or rule.title,
            symbol=ctx.symbol_for(lineno),
            context_confirmed=True,
        )


def _hardcoded_secret(ctx: FileContext, rule: Rule) -> Iterable[RuleHit]:
    pattern = re.compile(SECRET_NAME_PATTERN)
    for lineno, text in enumerate(ctx.lines, start=1):
        match = pattern.search(text)
        if not match:
            continue
        raw_value = match.group(2).strip("\"'")
        if not _is_secret_literal(raw_value):
            continue
        yield RuleHit(
            line=lineno,
            evidence=re.sub(r"([\"'])[^\"']{4,}\1", r"\1<secret>\1", text).strip()[:300],
            message="检测到疑似硬编码密钥/口令，应改为从环境变量或密钥管理服务读取。",
            symbol=ctx.symbol_for(lineno),
            context_confirmed=True,
        )


# ---------------------------------------------------------------------------
# AST 规则
# ---------------------------------------------------------------------------


def _sql_concat(ctx: FileContext, rule: Rule) -> Iterable[RuleHit]:
    if ctx.tree is None:
        return
    for node in ast.walk(ctx.tree):
        if not isinstance(node, ast.Call):
            continue
        name = call_name(node)
        if not name.endswith(("execute", "executemany", "raw", "text")):
            continue
        for arg in node.args:
            if _contains_sql_concat(arg):
                yield RuleHit(
                    line=node_line(node),
                    evidence=format_source(node)[:300],
                    message="SQL 语句由字符串拼接/格式化构造，存在注入风险，应改为参数化查询。",
                    symbol=ctx.symbol_for(node_line(node)),
                    context_confirmed=True,
                    dynamic_uncertain=_has_dynamic_name(arg),
                )
                break


def _contains_sql_concat(node: ast.AST) -> bool:
    if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Mod)):
        return _mentions_sql(node)
    if isinstance(node, ast.JoinedStr):
        return _mentions_sql(node)
    if isinstance(node, ast.Call):
        name = call_name(node)
        if name.endswith(("format", "join")) and _mentions_sql(node):
            return True
    if isinstance(node, ast.Name):
        return False
    return False


def _mentions_sql(node: ast.AST) -> bool:
    for child in ast.walk(node):
        text = string_value(child)
        if text and any(keyword in text.lower() for keyword in SQL_KEYWORDS):
            return True
    return False


def _has_dynamic_name(node: ast.AST) -> bool:
    for child in ast.walk(node):
        if isinstance(child, ast.Call) and call_name(child) in {"getattr", "__import__", "eval", "exec"}:
            return True
    return False


def _shell_true(ctx: FileContext, rule: Rule) -> Iterable[RuleHit]:
    if ctx.tree is None:
        return
    for node in ast.walk(ctx.tree):
        if not isinstance(node, ast.Call):
            continue
        name = call_name(node)
        if not name.startswith(("subprocess.", "os.")):
            continue
        if any(
            keyword.arg == "shell" and isinstance(keyword.value, ast.Constant) and keyword.value.value is True
            for keyword in node.keywords
        ):
            yield RuleHit(
                line=node_line(node),
                evidence=format_source(node)[:300],
                message="subprocess 使用 shell=True，命令拼接可导致命令注入，应改为参数列表调用。",
                symbol=ctx.symbol_for(node_line(node)),
                context_confirmed=True,
            )


def _mutable_default(ctx: FileContext, rule: Rule) -> Iterable[RuleHit]:
    if ctx.tree is None:
        return
    for node in ast.walk(ctx.tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for default in list(node.args.defaults) + [item for item in node.args.kw_defaults if item]:
            if isinstance(default, (ast.List, ast.Dict, ast.Set, ast.ListComp, ast.DictComp, ast.SetComp)):
                yield RuleHit(
                    line=node_line(node),
                    evidence=f"def {node.name}(... = {format_source(default)})"[:300],
                    message="函数默认参数使用了可变对象，会在调用间共享状态，应改为 None 并在函数体内初始化。",
                    symbol=node.name,
                    context_confirmed=True,
                )
                break


def _bare_except(ctx: FileContext, rule: Rule) -> Iterable[RuleHit]:
    if ctx.tree is None:
        return
    for node in ast.walk(ctx.tree):
        if isinstance(node, ast.ExceptHandler) and node.type is None:
            yield RuleHit(
                line=node_line(node),
                evidence=f"except:  # line {node_line(node)}",
                message="裸 except 会吞掉包括 KeyboardInterrupt 在内的所有异常，应捕获具体异常类型。",
                symbol=ctx.symbol_for(node_line(node)),
                context_confirmed=True,
            )


def _unclosed_resource(ctx: FileContext, rule: Rule) -> Iterable[RuleHit]:
    """检测没有使用 with 且未显式 close 的文件打开。"""
    if ctx.tree is None:
        return
    with_lines: set[int] = set()
    for node in ast.walk(ctx.tree):
        if isinstance(node, ast.With):
            for item in node.items:
                if isinstance(item.context_expr, ast.Call):
                    with_lines.add(node_line(item.context_expr))
    for node in ast.walk(ctx.tree):
        if not isinstance(node, ast.Call):
            continue
        if not call_name(node).endswith("open"):
            continue
        line = node_line(node)
        if line in with_lines:
            continue
        line_text = ctx.line_text(line)
        if "with " in line_text:
            continue
        yield RuleHit(
            line=line,
            evidence=line_text.strip()[:300],
            message="open() 未使用 with 语句，异常路径下文件句柄可能泄漏，应改为 with open(...)。",
            symbol=ctx.symbol_for(line),
            context_confirmed=True,
        )


def _assert_for_validation(ctx: FileContext, rule: Rule) -> Iterable[RuleHit]:
    """在非测试文件中用 assert 做输入校验（-O 下会被移除）。"""
    if ctx.tree is None or "test" in ctx.path.lower():
        return
    for node in ast.walk(ctx.tree):
        if isinstance(node, ast.Assert):
            yield RuleHit(
                line=node_line(node),
                evidence=format_source(node)[:300],
                message="使用 assert 做运行时校验：优化模式（python -O）下断言会被移除，应改为显式异常。",
                symbol=ctx.symbol_for(node_line(node)),
                context_confirmed=True,
            )


def _os_system_pattern(ctx: FileContext, rule: Rule) -> Iterable[RuleHit]:
    for lineno, text in enumerate(ctx.lines, start=1):
        if re.search(r"\bos\.system\s*\(|\bos\.popen\s*\(", text):
            yield RuleHit(
                line=lineno,
                evidence=text.strip()[:300],
                message="os.system/os.popen 直接执行命令字符串，应改为 subprocess 参数列表调用。",
                symbol=ctx.symbol_for(lineno),
                context_confirmed=True,
            )


def _insecure_random(ctx: FileContext, rule: Rule) -> Iterable[RuleHit]:
    """仅在安全语境内提示：随机数用于 token/密码/session 等场景。"""
    security_tokens = ("token", "password", "secret", "session", "salt", "otp", "nonce", "key")
    for lineno, text in enumerate(ctx.lines, start=1):
        if not re.search(r"\brandom\.(random|randint|randrange|choice|choices|sample)\s*\(", text):
            continue
        window = " ".join(ctx.lines[max(0, lineno - 6) : lineno + 3]).lower()
        if not any(token in window for token in security_tokens):
            continue
        yield RuleHit(
            line=lineno,
            evidence=text.strip()[:300],
            message="安全场景使用非加密随机源（random 模块），应改用 secrets / os.urandom。",
            symbol=ctx.symbol_for(lineno),
            context_confirmed=True,
        )


def _subprocess_missing_check(ctx: FileContext, rule: Rule) -> Iterable[RuleHit]:
    if ctx.tree is None:
        return
    for node in ast.walk(ctx.tree):
        if not isinstance(node, ast.Call):
            continue
        if call_name(node) not in {"subprocess.run", "subprocess.call", "subprocess.check_call"}:
            continue
        has_check = any(keyword.arg == "check" for keyword in node.keywords)
        has_shell = any(
            keyword.arg == "shell" and isinstance(keyword.value, ast.Constant) and keyword.value.value
            for keyword in node.keywords
        )
        if has_check or has_shell:
            continue
        yield RuleHit(
            line=node_line(node),
            evidence=format_source(node)[:300],
            message="subprocess 调用未检查返回码（check=True 或缺省异常处理），失败会被静默忽略。",
            symbol=ctx.symbol_for(node_line(node)),
            context_confirmed=True,
        )


def _broad_except(ctx: FileContext, rule: Rule) -> Iterable[RuleHit]:
    if ctx.tree is None:
        return
    for node in ast.walk(ctx.tree):
        if not isinstance(node, ast.ExceptHandler) or node.type is None:
            continue
        if isinstance(node.type, ast.Name) and node.type.id in {"Exception", "BaseException"}:
            yield RuleHit(
                line=node_line(node),
                evidence=f"except {node.type.id}:",
                message=f"捕获 {node.type.id} 过于宽泛，应捕获具体异常类型并保留上下文。",
                symbol=ctx.symbol_for(node_line(node)),
                context_confirmed=True,
            )


def _insecure_url_scheme(ctx: FileContext, rule: Rule) -> Iterable[RuleHit]:
    ignore = ("localhost", "127.0.0.1", "0.0.0.0", "example.com", "example.org", "schema", "w3.org", "testserver")
    for lineno, text in enumerate(ctx.lines, start=1):
        for match in re.finditer(r"[\"']http://([A-Za-z0-9._\-]+)", text):
            host = match.group(1).lower()
            if any(token in host for token in ignore):
                continue
            yield RuleHit(
                line=lineno,
                evidence=text.strip()[:300],
                message="使用明文 HTTP 访问远端服务，应改用 HTTPS 并校验证书。",
                symbol=ctx.symbol_for(lineno),
                context_confirmed=True,
            )


def _credential_in_url(ctx: FileContext, rule: Rule) -> Iterable[RuleHit]:
    placeholders = ("user:pass@", "username:password@", "${", "{{", "<", "***")
    pattern = re.compile(r"[a-zA-Z][a-zA-Z0-9+.\-]*://([^/\s:@'\"]+):([^/\s@'\"]+)@")
    for lineno, text in enumerate(ctx.lines, start=1):
        match = pattern.search(text)
        if not match:
            continue
        if any(token in text for token in placeholders):
            continue
        yield RuleHit(
            line=lineno,
            evidence=re.sub(r"://[^@\s]+@", "://<redacted>@", text).strip()[:300],
            message="连接串中直接内联了账号口令，应改为从环境变量或密钥管理服务读取。",
            symbol=ctx.symbol_for(lineno),
            context_confirmed=True,
        )


def _yaml_unsafe_load(ctx: FileContext, rule: Rule) -> Iterable[RuleHit]:
    if ctx.tree is None:
        return
    for node in ast.walk(ctx.tree):
        if not isinstance(node, ast.Call):
            continue
        name = call_name(node)
        if name.endswith("yaml.load"):
            safe = any(
                keyword.arg == "Loader" and "Safe" in format_source(keyword.value)
                for keyword in node.keywords
            )
            if safe:
                continue
            yield RuleHit(
                line=node_line(node),
                evidence=format_source(node)[:300],
                message="yaml.load 未指定 SafeLoader，可被构造成任意对象，应使用 yaml.safe_load。",
                symbol=ctx.symbol_for(node_line(node)),
                context_confirmed=True,
            )
        elif name.endswith(("yaml.load_all",)):
            yield RuleHit(
                line=node_line(node),
                evidence=format_source(node)[:300],
                message="yaml.load_all 默认使用不安全 Loader，应改为 safe_load_all。",
                symbol=ctx.symbol_for(node_line(node)),
                context_confirmed=True,
            )


# ---------------------------------------------------------------------------
# 规则清单（阶段三基线；阶段四扩展到 20+ 条）
# ---------------------------------------------------------------------------

RULES: tuple[Rule, ...] = (
    Rule(
        rule_id="R001_SQL_CONCAT",
        title="SQL 字符串拼接",
        cwe="CWE-89",
        severity=Severity.CRITICAL,
        confidence_base=0.80,
        kind=RuleKind.AST_DATAFLOW,
        check="sql_concat",
        auto_fixable=True,
        fix_category=FixCategory.SQL_PARAMETERIZATION,
        description="SQL 语句由字符串拼接或格式化构造，存在注入风险。",
    ),
    Rule(
        rule_id="R002_HARDCODED_SECRET",
        title="硬编码密钥",
        cwe="CWE-798",
        severity=Severity.CRITICAL,
        confidence_base=0.80,
        kind=RuleKind.REGEX,
        pattern=SECRET_NAME_PATTERN,
        check="hardcoded_secret",
        auto_fixable=True,
        fix_category=FixCategory.HARDCODED_SECRET,
        description="检测到疑似硬编码密钥/口令，应改为从环境变量或密钥管理服务读取。",
    ),
    Rule(
        rule_id="R003_SHELL_TRUE",
        title="shell=True 命令注入",
        cwe="CWE-78",
        severity=Severity.CRITICAL,
        confidence_base=0.85,
        kind=RuleKind.AST,
        check="shell_true",
        auto_fixable=True,
        fix_category=FixCategory.SHELL_TRUE,
        description="subprocess 使用 shell=True，应改为参数列表调用。",
    ),
    Rule(
        rule_id="R004_OS_SYSTEM",
        title="os.system 命令执行",
        cwe="CWE-78",
        severity=Severity.CRITICAL,
        confidence_base=0.75,
        kind=RuleKind.REGEX,
        pattern=r"\bos\.(system|popen)\s*\(",
        description="os.system/os.popen 直接执行命令字符串。",
    ),
    Rule(
        rule_id="R005_EVAL_EXEC",
        title="动态代码执行",
        cwe="CWE-95",
        severity=Severity.CRITICAL,
        confidence_base=0.70,
        kind=RuleKind.REGEX,
        pattern=r"(?<![\w.])(eval|exec)\s*\(",
        description="eval/exec 执行动态代码，可能导致任意代码执行。",
    ),
    Rule(
        rule_id="R006_INSECURE_DESERIALIZE",
        title="不安全反序列化",
        cwe="CWE-502",
        severity=Severity.CRITICAL,
        confidence_base=0.75,
        kind=RuleKind.REGEX,
        pattern=r"\b(pickle\.loads?|cPickle\.loads?|marshal\.loads)\s*\(",
        description="使用 pickle/marshal 反序列化不可信数据，可能执行任意对象构造。",
    ),
    Rule(
        rule_id="R007_MUTABLE_DEFAULT",
        title="可变默认参数",
        cwe="CWE-1188",
        severity=Severity.WARNING,
        confidence_base=0.70,
        kind=RuleKind.AST,
        check="mutable_default",
        description="函数默认参数使用可变对象，会产生跨调用共享状态。",
    ),
    Rule(
        rule_id="R008_BARE_EXCEPT",
        title="裸 except",
        cwe="CWE-396",
        severity=Severity.WARNING,
        confidence_base=0.65,
        kind=RuleKind.AST,
        check="bare_except",
        description="裸 except 吞掉所有异常，掩盖真实错误。",
    ),
    Rule(
        rule_id="R009_PATH_TRAVERSAL",
        title="路径拼接穿越",
        cwe="CWE-22",
        severity=Severity.WARNING,
        confidence_base=0.60,
        kind=RuleKind.REGEX,
        pattern=r"open\s*\([^)]*(\+|%|\.format|f[\"'])[^)]*\.\.[\\/]",
        description="文件路径由外部输入拼接且包含上溯片段。",
    ),
    Rule(
        rule_id="R010_WEAK_HASH",
        title="弱哈希算法",
        cwe="CWE-327",
        severity=Severity.WARNING,
        confidence_base=0.60,
        kind=RuleKind.REGEX,
        pattern=r"\b(hashlib\.)?(md5|sha1)\s*\(",
        description="使用 MD5/SHA1 等弱哈希算法。",
    ),
    Rule(
        rule_id="R011_UNCLOSED_RESOURCE",
        title="资源未关闭",
        cwe="CWE-772",
        severity=Severity.WARNING,
        confidence_base=0.55,
        kind=RuleKind.AST,
        check="unclosed_resource",
        description="open() 未使用 with 语句，异常路径下句柄可能泄漏。",
    ),
    Rule(
        rule_id="R012_ASSERT_VALIDATION",
        title="使用 assert 做校验",
        cwe="CWE-617",
        severity=Severity.INFO,
        confidence_base=0.55,
        kind=RuleKind.AST,
        check="assert_for_validation",
        description="assert 在优化模式下会被移除，不应用于输入校验。",
    ),
    Rule(
        rule_id="R013_INSECURE_TEMP_FILE",
        title="不安全的临时文件",
        cwe="CWE-377",
        severity=Severity.WARNING,
        confidence_base=0.70,
        kind=RuleKind.REGEX,
        pattern=r"\btempfile\.mktemp\s*\(",
        description="tempfile.mktemp 存在竞态条件，应使用 NamedTemporaryFile / mkstemp。",
    ),
    Rule(
        rule_id="R014_TLS_VERIFY_DISABLED",
        title="关闭 TLS 校验",
        cwe="CWE-295",
        severity=Severity.CRITICAL,
        confidence_base=0.80,
        kind=RuleKind.REGEX,
        pattern=(
            r"(verify\s*=\s*False|_create_unverified_context\s*\(|ssl\.CERT_NONE"
            r"|check_hostname\s*=\s*False)"
        ),
        description="关闭证书校验会导致中间人攻击，必须保留 TLS 校验。",
    ),
    Rule(
        rule_id="R015_INSECURE_RANDOM",
        title="安全场景使用非加密随机源",
        cwe="CWE-338",
        severity=Severity.WARNING,
        confidence_base=0.60,
        kind=RuleKind.AST,
        check="insecure_random",
        description="token、口令、会话标识等安全值不得使用 random 模块生成。",
    ),
    Rule(
        rule_id="R016_DEBUG_MODE_ENABLED",
        title="调试模式开启",
        cwe="CWE-489",
        severity=Severity.WARNING,
        confidence_base=0.70,
        kind=RuleKind.REGEX,
        pattern=r"(?m)^\s*DEBUG\s*=\s*True\b",
        description="生产代码中开启 DEBUG 会暴露堆栈与内部信息，应改为配置驱动。",
    ),
    Rule(
        rule_id="R017_CORS_WILDCARD",
        title="跨域来源通配",
        cwe="CWE-942",
        severity=Severity.WARNING,
        confidence_base=0.65,
        kind=RuleKind.REGEX,
        pattern=r"(allow_origins|ALLOWED_HOSTS)\s*=\s*\[\s*[\"']\*[\"']",
        description="CORS/主机白名单使用通配符会放大跨域风险，应显式列出可信来源。",
    ),
    Rule(
        rule_id="R018_SUBPROCESS_MISSING_CHECK",
        title="子进程返回码未检查",
        cwe="CWE-754",
        severity=Severity.INFO,
        confidence_base=0.55,
        kind=RuleKind.AST,
        check="subprocess_missing_check",
        description="subprocess 调用未设置 check=True，失败会被静默忽略。",
    ),
    Rule(
        rule_id="R019_BROAD_EXCEPT",
        title="过宽异常捕获",
        cwe="CWE-396",
        severity=Severity.WARNING,
        confidence_base=0.60,
        kind=RuleKind.AST,
        check="broad_except",
        description="捕获 Exception/BaseException 会掩盖真实错误，应捕获具体异常。",
    ),
    Rule(
        rule_id="R020_INSECURE_URL_SCHEME",
        title="明文 HTTP 端点",
        cwe="CWE-319",
        severity=Severity.WARNING,
        confidence_base=0.55,
        kind=RuleKind.AST,
        check="insecure_url_scheme",
        description="使用 http:// 访问远端服务会明文传输数据。",
    ),
    Rule(
        rule_id="R021_WORLD_WRITABLE_PERMISSION",
        title="文件权限过宽",
        cwe="CWE-732",
        severity=Severity.WARNING,
        confidence_base=0.65,
        kind=RuleKind.REGEX,
        pattern=r"\.chmod\s*\([^)]*\b0?o?777\b",
        description="chmod 777 使任意用户可写，应使用最小权限。",
    ),
    Rule(
        rule_id="R022_YAML_UNSAFE_LOAD",
        title="不安全 YAML 加载",
        cwe="CWE-502",
        severity=Severity.CRITICAL,
        confidence_base=0.75,
        kind=RuleKind.AST,
        check="yaml_unsafe_load",
        description="yaml.load 未指定 SafeLoader，可构造任意对象。",
    ),
    Rule(
        rule_id="R023_XML_UNSAFE_PARSE",
        title="XML 外部实体风险",
        cwe="CWE-611",
        severity=Severity.WARNING,
        confidence_base=0.55,
        kind=RuleKind.REGEX,
        pattern=r"\b(?:xml\.etree\.ElementTree|ElementTree|ET)\.(parse|fromstring|XML)\s*\(",
        description="标准库 XML 解析未禁用外部实体，存在 XXE 风险，应使用 defusedxml。",
    ),
    Rule(
        rule_id="R024_CREDENTIAL_IN_URL",
        title="连接串内联凭据",
        cwe="CWE-798",
        severity=Severity.CRITICAL,
        confidence_base=0.75,
        kind=RuleKind.AST,
        check="credential_in_url",
        description="连接串中内联账号口令会被写入版本库，应改用环境变量。",
    ),
)


@register_check("sql_concat")
def _check_sql_concat(ctx: FileContext, rule: Rule) -> Iterable[RuleHit]:
    return _sql_concat(ctx, rule)


@register_check("shell_true")
def _check_shell_true(ctx: FileContext, rule: Rule) -> Iterable[RuleHit]:
    return _shell_true(ctx, rule)


@register_check("mutable_default")
def _check_mutable_default(ctx: FileContext, rule: Rule) -> Iterable[RuleHit]:
    return _mutable_default(ctx, rule)


@register_check("bare_except")
def _check_bare_except(ctx: FileContext, rule: Rule) -> Iterable[RuleHit]:
    return _bare_except(ctx, rule)


@register_check("unclosed_resource")
def _check_unclosed_resource(ctx: FileContext, rule: Rule) -> Iterable[RuleHit]:
    return _unclosed_resource(ctx, rule)


@register_check("assert_for_validation")
def _check_assert(ctx: FileContext, rule: Rule) -> Iterable[RuleHit]:
    return _assert_for_validation(ctx, rule)


@register_check("hardcoded_secret")
def _check_hardcoded_secret(ctx: FileContext, rule: Rule) -> Iterable[RuleHit]:
    return _hardcoded_secret(ctx, rule)


@register_check("insecure_random")
def _check_insecure_random(ctx: FileContext, rule: Rule) -> Iterable[RuleHit]:
    return _insecure_random(ctx, rule)


@register_check("subprocess_missing_check")
def _check_subprocess_missing_check(ctx: FileContext, rule: Rule) -> Iterable[RuleHit]:
    return _subprocess_missing_check(ctx, rule)


@register_check("broad_except")
def _check_broad_except(ctx: FileContext, rule: Rule) -> Iterable[RuleHit]:
    return _broad_except(ctx, rule)


@register_check("insecure_url_scheme")
def _check_insecure_url_scheme(ctx: FileContext, rule: Rule) -> Iterable[RuleHit]:
    return _insecure_url_scheme(ctx, rule)


@register_check("yaml_unsafe_load")
def _check_yaml_unsafe_load(ctx: FileContext, rule: Rule) -> Iterable[RuleHit]:
    return _yaml_unsafe_load(ctx, rule)


@register_check("credential_in_url")
def _check_credential_in_url(ctx: FileContext, rule: Rule) -> Iterable[RuleHit]:
    return _credential_in_url(ctx, rule)


CHECK_DISPATCH = {
    "sql_concat": _sql_concat,
    "shell_true": _shell_true,
    "mutable_default": _mutable_default,
    "bare_except": _bare_except,
    "unclosed_resource": _unclosed_resource,
    "assert_for_validation": _assert_for_validation,
    "insecure_random": _insecure_random,
    "subprocess_missing_check": _subprocess_missing_check,
    "broad_except": _broad_except,
    "insecure_url_scheme": _insecure_url_scheme,
    "yaml_unsafe_load": _yaml_unsafe_load,
    "credential_in_url": _credential_in_url,
}


def rule_by_id(rule_id: str) -> Rule | None:
    for rule in RULES:
        if rule.rule_id == rule_id:
            return rule
    return None


__all__ = [
    "RULES",
    "SECRET_NAME_PATTERN",
    "_hardcoded_secret",
    "_os_system_pattern",
    "_regex_hits",
    "rule_by_id",
]
