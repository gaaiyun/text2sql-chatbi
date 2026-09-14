"""命令行入口。

    python -m text2sql ask "广州市存续企业有多少家"     单次提问，输出 Markdown 报告
    python -m text2sql chat                           多轮对话（同一会话内可以追问）
    python -m text2sql eval --gate                    跑评测集，指标不达标时退出码为 1
    python -m text2sql serve                          启动 HTTP API
    python -m text2sql mcp                            以 MCP server 形式暴露给其他智能体
    python -m text2sql build-demo | catalog | doctor

退出码：0 成功；1 问题未回答或评测门禁未通过；2 参数或配置错误。
"""

from __future__ import annotations

import argparse
import contextlib
import json
import sys
import uuid
from collections.abc import Sequence
from dataclasses import asdict
from datetime import date
from pathlib import Path
from typing import Any

from text2sql import __version__
from text2sql.config import REPO_ROOT, Settings

PLANNERS: dict[str, tuple[str, ...]] = {
    "auto": ("semantic", "llm"),
    "semantic": ("semantic",),
    "llm": ("llm",),
}
DEFAULT_REPORT = REPO_ROOT / "docs" / "EVALUATION.md"
EXIT_OK, EXIT_NOT_ANSWERED, EXIT_USAGE = 0, 1, 2
_MODEL_HINT = "请设置 OPENAI_API_KEY（或 LLM_PROVIDER=deepseek 与 DEEPSEEK_API_KEY）"


class CLIError(Exception):
    """参数或配置问题：打印原因，以退出码 2 结束。"""


# --------------------------------------------------------------------------- 公共


def _dumps(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2, default=str)


def _load_settings() -> Settings:
    try:
        return Settings.from_env()
    except ValueError as exc:
        raise CLIError(str(exc)) from exc


def _build_agent(settings: Settings, planner: str, *, require_llm: bool = False):
    from text2sql.agent.graph import Text2SQLAgent

    if (require_llm or planner == "llm") and settings.llm is None:
        raise CLIError(f"--planner {planner} 需要模型配置：{_MODEL_HINT}")
    return Text2SQLAgent.from_settings(settings, planners=PLANNERS[planner])


def _format_event(event: dict[str, Any]) -> str:
    if event.get("type") == "agent_step":
        mark = "ok" if event.get("ok") else "error"
        return (
            f"  [agent] #{event.get('index')} {event.get('tool')} {mark} "
            f"{float(event.get('latency_ms') or 0):.0f}ms {event.get('summary', '')}"
        ).rstrip()
    parts = []
    for key, value in (event.get("detail") or {}).items():
        if isinstance(value, list) and all(isinstance(v, str | int | float) for v in value):
            value = ",".join(str(v) for v in value)
        if isinstance(value, str | int | float) and value != "":
            parts.append(f"{key}={value}")
    brief = " ".join(parts)
    return f"[{event.get('node')}] {event.get('status')} {float(event.get('ms') or 0):.1f}ms {brief}".rstrip()


def _text_table(columns: list[str], rows: list[dict[str, Any]]) -> str:
    lines = [" | ".join(columns)]
    lines += [
        " | ".join("" if row.get(c) is None else str(row.get(c)) for c in columns) for row in rows
    ]
    return "\n".join(lines)


# --------------------------------------------------------------------------- ask / chat


def _cmd_ask(args: argparse.Namespace) -> int:
    agent = _build_agent(_load_settings(), args.planner)
    try:
        if args.stream:
            payload: dict[str, Any] = {}
            for event in agent.stream(args.question, thread_id=args.thread):
                if event.get("type") == "result":
                    payload = event["result"]
                else:
                    print(_format_event(event), file=sys.stderr, flush=True)
        else:
            payload = agent.ask(args.question, thread_id=args.thread).to_dict()
    finally:
        agent.close()

    print(_dumps(payload) if args.json else payload["report"])
    return EXIT_OK if payload.get("status") == "answered" else EXIT_NOT_ANSWERED


def _render_turn(result: Any) -> str:
    from text2sql.agent.graph import PLANNER_LABELS, STATUS_LABELS

    lines = []
    if result.effective_question and result.effective_question != result.question:
        lines.append(f"（已理解为：{result.effective_question}）")
    lines.append(result.answer or result.message or "")
    if result.columns and result.rows:
        preview = result.rows[:10]
        lines.append(_text_table(result.columns, preview))
        if result.row_count > len(preview):
            lines.append(f"……共 {result.row_count} 行")
    meta = [STATUS_LABELS.get(result.status, result.status)]
    if result.planner:
        meta.append(PLANNER_LABELS.get(result.planner, result.planner))
    meta.append(f"{result.elapsed_ms:.0f} ms")
    lines.append("[" + " | ".join(meta) + "]")
    return "\n".join(line for line in lines if line)


def _cmd_chat(args: argparse.Namespace) -> int:
    agent = _build_agent(_load_settings(), args.planner)
    thread = f"cli-{uuid.uuid4().hex[:12]}"
    last_sql: str | None = None
    planners = "、".join(agent.planners)
    print(f"Text2SQL 对话（数据源 {agent.backend.name}，规划器 {planners}）")
    print("直接输入问题，可以接着追问；/new 开始新会话，/sql 查看上一条 SQL，/exit 退出。")
    try:
        while True:
            try:
                question = input("\n> ").strip()
            except (EOFError, KeyboardInterrupt):
                break
            if not question:
                continue
            if question in ("/exit", "/quit"):
                break
            if question == "/new":
                thread = f"cli-{uuid.uuid4().hex[:12]}"
                print("已开始新会话。")
                continue
            if question == "/sql":
                print(last_sql or "上一轮没有生成 SQL。")
                continue
            result = agent.ask(question, thread_id=thread)
            last_sql = result.safe_sql or result.sql or last_sql
            print(_render_turn(result))
    finally:
        agent.close()
    return EXIT_OK


# --------------------------------------------------------------------------- eval


def _outcome_marker(outcome: Any) -> str:
    if outcome.expect == "refuse":
        return "[OK]  " if outcome.refused_ok else "[BAD] "
    if outcome.correct is None:
        return "[--]  "
    return "[OK]  " if outcome.correct else "[BAD] "


def _cmd_eval(args: argparse.Namespace) -> int:
    from text2sql.evaluation.runner import (
        MODE_LABELS,
        check_gate,
        load_benchmark,
        render_markdown,
        replace_marked_section,
        run_evaluation,
    )

    items = load_benchmark(args.benchmark)
    if args.ids:
        wanted = [token.strip() for token in args.ids.split(",") if token.strip()]
        known = {item.id for item in items}
        unknown = [token for token in wanted if token not in known]
        if unknown:
            raise CLIError(f"评测集中没有这些题号：{', '.join(unknown)}")
        position = {token: index for index, token in enumerate(wanted)}
        items = sorted((i for i in items if i.id in position), key=lambda i: position[i.id])

    settings = _load_settings()
    agent = _build_agent(settings, args.planner, require_llm=args.planner != "semantic")

    def show(outcome: Any) -> None:
        if not args.quiet:
            print(
                f"{_outcome_marker(outcome)}{outcome.id:<4} {outcome.status:<9} "
                f"{outcome.latency_ms:>7.0f}ms  {outcome.question}",
                flush=True,
            )

    try:
        summary, outcomes = run_evaluation(
            agent, items, backend=agent.backend, mode=args.planner, on_item=show
        )
        backend_name = agent.backend.name
    finally:
        agent.close()

    label = MODE_LABELS.get(args.planner, args.planner)
    print(
        f"\n{label}：题目 {summary.total} | 覆盖率 {summary.coverage:.1%} | "
        f"作答精确率 {summary.precision:.1%} | 执行准确率 {summary.execution_accuracy:.1%} | "
        f"拒答准确率 {summary.refusal_accuracy:.1%} | Schema 召回率 {summary.linking_recall:.1%} | "
        f"P50 {summary.latency_p50_ms:.0f}ms / P95 {summary.latency_p95_ms:.0f}ms"
    )
    if summary.tokens:
        print(f"模型调用 {summary.llm_calls} 次，共 {summary.tokens} tokens")

    meta = {
        "date": date.today().isoformat(),
        "backend": backend_name,
        "planner": args.planner,
        "model": settings.llm.model if settings.llm and args.planner != "semantic" else None,
        "version": __version__,
        "benchmark": Path(args.benchmark).name,
    }
    if args.json:
        target = Path(args.json)
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "meta": meta,
            "summary": summary.to_dict(),
            "outcomes": [asdict(o) for o in outcomes],
        }
        target.write_text(_dumps(payload) + "\n", encoding="utf-8")
        print(f"已写入 {target}")
    if args.write_report:
        report = Path(args.report)
        document = report.read_text(encoding="utf-8") if report.exists() else "# 评测结果\n"
        block = render_markdown(summary, outcomes, meta)
        report.write_text(replace_marked_section(document, args.planner, block), encoding="utf-8")
        print(f"已更新 {report}（区块 EVAL:{args.planner}）")

    if not args.gate:
        return EXIT_OK
    failures = check_gate(
        summary,
        min_precision=args.min_precision,
        min_refusal=args.min_refusal,
        min_coverage=args.min_coverage,
    )
    for failure in failures:
        print(f"[FAIL] {failure}")
    if failures:
        return EXIT_NOT_ANSWERED
    print(
        f"[PASS] 评测门禁通过：作答精确率 >= {args.min_precision:.0%}，"
        f"拒答准确率 >= {args.min_refusal:.0%}，覆盖率 >= {args.min_coverage:.0%}"
    )
    return EXIT_OK


# --------------------------------------------------------------------------- 服务


def _cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    uvicorn.run(
        "text2sql.api.app:create_app",
        factory=True,
        host=args.host,
        port=args.port,
        reload=args.reload,
    )
    return EXIT_OK


def _cmd_mcp(args: argparse.Namespace) -> int:
    # stdio 传输时标准输出只能写协议消息，这里不能打印任何提示
    from text2sql.mcp_server import build_server

    server = build_server()
    if args.transport == "stdio":
        server.run("stdio")
    else:
        server.run(args.transport, host=args.host, port=args.port)
    return EXIT_OK


# --------------------------------------------------------------------------- 维护


def _cmd_build_demo(args: argparse.Namespace) -> int:
    import time

    import duckdb

    from text2sql.db.demo import DEMO_VERSION, build_demo_database, read_demo_version
    from text2sql.db.schema import load_znjz_schema

    target = Path(args.path) if args.path else _load_settings().demo_db_path
    if target.exists() and not args.force and read_demo_version(target) == DEMO_VERSION:
        print(f"[OK] 演示库已是最新版本 {DEMO_VERSION}：{target}")
        return EXIT_OK

    started = time.perf_counter()
    build_demo_database(target)
    elapsed = time.perf_counter() - started
    print(f"[OK] 已生成演示库 {target}（版本 {DEMO_VERSION}，{elapsed:.1f}s，")
    print(f"     {target.stat().st_size / 1024 / 1024:.1f} MB）")
    connection = duckdb.connect(str(target), read_only=True)
    try:
        for table in load_znjz_schema().tables.values():
            count = connection.execute(f'SELECT COUNT(*) FROM "{table.name}"').fetchone()[0]
            kind = "视图" if table.kind == "view" else "表"
            print(f"  {kind} {table.name}: {count} 行")
    finally:
        connection.close()
    return EXIT_OK


def _cmd_catalog(args: argparse.Namespace) -> int:
    from text2sql.db.schema import load_znjz_schema
    from text2sql.semantic.catalog import load_znjz_catalog

    catalog = load_znjz_catalog()
    summary = catalog.summary()
    if args.json:
        print(_dumps(summary))
        return EXIT_OK

    def names(section: str) -> str:
        return "、".join(item["label"] for item in summary[section])

    need_llm = sum(1 for e in summary["examples"] if e["requires_llm"])
    print(
        f"语义层：{summary['title']}（{summary['dataset']}，数据截至 {summary['anchor_year']} 年）"
    )
    print(f"  实体 {len(summary['entities'])}：{names('entities')}")
    print(f"  指标 {len(summary['metrics'])}：{names('metrics')}")
    print(f"  维度 {len(summary['dimensions'])}：{names('dimensions')}")
    print(f"  筛选 {len(summary['filters'])}：{names('filters')}")
    print(f"  值映射 {len(summary['value_maps'])}：{names('value_maps')}")
    print(f"  示例问题 {len(summary['examples'])}（其中 {need_llm} 条需要模型）")

    schema = load_znjz_schema()
    problems = catalog.validate(schema)
    for problem in problems:
        print(f"[FAIL] {problem}")
    ambiguous = catalog.ambiguous_surfaces()
    for word, targets in ambiguous.items():
        print(f"[WARN] “{word}”同时指向 {', '.join(targets)}")
    if problems:
        return EXIT_NOT_ANSWERED
    tables = sum(1 for t in schema.tables.values() if t.kind != "view")
    views = len(schema.tables) - tables
    print(f"[OK] 语义层与生产 DDL 一致（{tables} 张表、{views} 个视图）")
    return EXIT_OK


def _cmd_doctor(args: argparse.Namespace) -> int:
    from text2sql.db.demo import read_demo_version

    settings = _load_settings()
    print(f"text2sql {__version__}，Python {sys.version.split()[0]}")
    if settings.database == "demo":
        path = settings.demo_db_path
        version = read_demo_version(path) if path.exists() else None
        state = f"版本 {version}" if version else "尚未生成，首次运行时自动构建"
        print(f"数据源：合成演示库 {path}（{state}）")
    else:
        assert settings.mysql is not None
        print(f"数据源：MySQL 库 {settings.mysql.database}（只读会话，主机信息不在此展示）")
    if settings.llm:
        llm = settings.llm.describe()
        print(f"模型：{llm['provider']} / {llm['model']}（{llm['base_url']}）")
    else:
        print(f"模型：未配置，只启用语义层路径；{_MODEL_HINT}后可启用 SQL 智能体")

    try:
        agent = _build_agent(settings, "auto")
    except Exception as exc:  # noqa: BLE001 - 体检命令要把任何初始化失败都报告出来
        print(f"[FAIL] 初始化失败：{type(exc).__name__}: {exc}")
        return EXIT_NOT_ANSWERED
    try:
        status = agent.status()
        agent.backend.execute("SELECT 1 AS ok", max_rows=1)
    except Exception as exc:  # noqa: BLE001
        print(f"[FAIL] 数据库不可用：{type(exc).__name__}: {exc}")
        return EXIT_NOT_ANSWERED
    finally:
        agent.close()
    index = status["value_index"]
    print(f"[OK] 数据库连接正常（{status['backend']}）")
    print(
        f"[OK] 值索引 {index['columns']} 列，跳过 {index['skipped']} 列，用时 {index['ms']:.0f} ms"
    )
    print(
        f"[OK] 规划器 {'、'.join(status['planners'])}，SQL 智能体{'已启用' if status['sql_agent'] else '未启用'}"
    )
    print(f"[OK] 工作流 {len(status['nodes'])} 个节点，检查点 {status['checkpointer']}")
    return EXIT_OK


# --------------------------------------------------------------------------- 参数


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="text2sql", description="智能制造企业数据库的 Text2SQL 分析智能体"
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)

    def planner_option(sub: argparse.ArgumentParser, default: str) -> None:
        sub.add_argument(
            "--planner",
            choices=sorted(PLANNERS),
            default=default,
            help="semantic 只用语义层；llm 只用 SQL 智能体；auto 语义层优先、模型兜底",
        )

    ask = commands.add_parser("ask", help="提一个问题")
    ask.add_argument("question")
    planner_option(ask, "auto")
    ask.add_argument("--json", action="store_true", help="输出完整 JSON 结果")
    ask.add_argument("--stream", action="store_true", help="在标准错误输出上实时打印节点进度")
    ask.add_argument("--thread", help="会话 ID；同一 ID 下可以追问")
    ask.set_defaults(handler=_cmd_ask)

    chat = commands.add_parser("chat", help="多轮对话")
    planner_option(chat, "auto")
    chat.set_defaults(handler=_cmd_chat)

    evaluate = commands.add_parser("eval", help="运行评测集")
    planner_option(evaluate, "semantic")
    evaluate.add_argument("--benchmark", default=None, help="评测集 JSONL 路径")
    evaluate.add_argument("--ids", help="只跑指定题号，逗号分隔，例如 b01,b02,r01")
    evaluate.add_argument("--gate", action="store_true", help="指标低于门槛时退出码为 1")
    evaluate.add_argument("--min-precision", type=float, default=0.95)
    evaluate.add_argument("--min-refusal", type=float, default=1.0)
    evaluate.add_argument("--min-coverage", type=float, default=0.6)
    evaluate.add_argument("--json", help="把逐题结果写入 JSON 文件")
    evaluate.add_argument("--write-report", action="store_true", help="更新评测文档里对应区块")
    evaluate.add_argument("--report", default=str(DEFAULT_REPORT))
    evaluate.add_argument("--quiet", action="store_true", help="不打印逐题结果")
    evaluate.set_defaults(handler=_cmd_eval)

    serve = commands.add_parser("serve", help="启动 HTTP API")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--reload", action="store_true")
    serve.set_defaults(handler=_cmd_serve)

    mcp = commands.add_parser("mcp", help="启动 MCP server")
    mcp.add_argument("--transport", choices=("stdio", "streamable-http", "sse"), default="stdio")
    mcp.add_argument("--host", default="127.0.0.1")
    mcp.add_argument("--port", type=int, default=8765)
    mcp.set_defaults(handler=_cmd_mcp)

    build = commands.add_parser("build-demo", help="生成合成演示库")
    build.add_argument("--path", help="输出路径，默认取 T2S_DEMO_DB_PATH")
    build.add_argument("--force", action="store_true", help="即使版本一致也重新生成")
    build.set_defaults(handler=_cmd_build_demo)

    catalog = commands.add_parser("catalog", help="查看并校验语义层")
    catalog.add_argument("--json", action="store_true")
    catalog.set_defaults(handler=_cmd_catalog)

    doctor = commands.add_parser("doctor", help="检查配置、数据库和模型设置")
    doctor.set_defaults(handler=_cmd_doctor)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    # Windows 控制台可能是 GBK，个别字符编码失败时替换成问号，而不是让整个命令崩溃
    for stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(AttributeError, ValueError):
            stream.reconfigure(errors="replace")
    args = build_parser().parse_args(argv)
    if getattr(args, "benchmark", "unset") is None:
        from text2sql.evaluation.runner import ZNJZ_BENCHMARK_PATH

        args.benchmark = str(ZNJZ_BENCHMARK_PATH)
    try:
        return args.handler(args)
    except CLIError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return EXIT_USAGE
