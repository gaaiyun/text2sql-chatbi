"""部署前检查：不读取任何真实密钥，只核对仓库里的部署约定是否齐全、彼此一致。

    python scripts/check_deploy_readiness.py [--json]

覆盖三种部署方式：Cloudflare Workers 站点、Docker（接口 + 界面）、Streamlit Cloud。
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# text2sql/config.py 读取的配置项；新增配置时同步这里，tests/test_deploy_readiness.py 会核对
CONFIG_KEYS = (
    "LLM_PROVIDER",
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "OPENAI_MODEL",
    "DEEPSEEK_API_KEY",
    "DEEPSEEK_BASE_URL",
    "DEEPSEEK_MODEL",
    "MODEL_TEMPERATURE",
    "LLM_TIMEOUT",
    "T2S_AGENT_MAX_STEPS",
    "APP_PASSWORD",
    "T2S_DATABASE",
    "T2S_DEMO_DB_PATH",
    "DB_HOST_SCENARIO_1_3",
    "DB_PORT_SCENARIO_1_3",
    "DB_NAME_SCENARIO_1_3",
    "DB_USER_SCENARIO_1_3",
    "DB_PASSWORD_SCENARIO_1_3",
    "T2S_MAX_ROWS",
    "T2S_MAX_REPAIRS",
    "T2S_QUERY_TIMEOUT",
    "T2S_COST_LIMIT",
    "T2S_LLM_NARRATIVE",
    "T2S_ALLOWED_ORIGINS",
    "T2S_RATE_LIMIT",
)
REQUIRED_FILES = (
    "pyproject.toml",
    "requirements.txt",
    "streamlit_app.py",
    "api_server.py",
    "Dockerfile",
    "docker-compose.yml",
    ".dockerignore",
    ".env.example",
    ".streamlit/secrets.toml.example",
    "docs/DEPLOYMENT.md",
    "site/wrangler.jsonc",
    "site/worker/index.js",
    "site/src/_headers",
    "scripts/build_site.py",
)
IGNORED = (".env", ".streamlit/secrets.toml", "site/dist/", ".wrangler/", "data/demo/")
NEVER_TRACKED = (".env", ".streamlit/secrets.toml")


@dataclass(frozen=True)
class CheckResult:
    name: str
    ok: bool
    detail: str


def _read(root: Path, relative: str) -> str:
    path = root / relative
    return path.read_text(encoding="utf-8") if path.is_file() else ""


def _tracked(root: Path) -> set[str]:
    try:
        output = subprocess.run(
            ["git", "ls-files"], cwd=root, check=True, capture_output=True, text=True
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        return set()
    return set(output.splitlines())


def run_checks(root: Path = ROOT) -> list[CheckResult]:
    root = root.resolve()
    results: list[CheckResult] = []

    def check(name: str, ok: bool, good: str, bad: str) -> None:
        results.append(CheckResult(name, bool(ok), good if ok else bad))

    for relative in REQUIRED_FILES:
        check(f"file:{relative}", (root / relative).is_file(), "存在", "缺失")

    gitignore = _read(root, ".gitignore")
    for pattern in IGNORED:
        check(f"gitignore:{pattern}", pattern in gitignore, "已忽略", "未写入 .gitignore")
    tracked = _tracked(root)
    for relative in NEVER_TRACKED:
        check(f"untracked:{relative}", relative not in tracked, "未入库", "被 Git 跟踪了")

    env_example = _read(root, ".env.example")
    deployment = _read(root, "docs/DEPLOYMENT.md")
    for key in CONFIG_KEYS:
        check(f"env_example:{key}", key in env_example, "有说明", ".env.example 里没有")
        check(f"deployment_doc:{key}", key in deployment, "有说明", "docs/DEPLOYMENT.md 里没有")

    wrangler = _read(root, "site/wrangler.jsonc")
    check(
        "wrangler:assets",
        '"directory": "./dist"' in wrangler,
        "静态资源来自 dist",
        "assets.directory 不是 ./dist",
    )
    check(
        "wrangler:api_first",
        '"/api/*"' in wrangler,
        "/api/* 先进入 Worker",
        "缺少 run_worker_first",
    )
    check("wrangler:ai_binding", '"binding": "AI"' in wrangler, "绑定 Workers AI", "缺少 AI 绑定")
    check("wrangler:rate_limit", "LLM_LIMITER" in wrangler, "模型接口有限流", "缺少限流绑定")

    worker = _read(root, "site/worker/index.js")
    check("worker:same_origin", "sameOrigin(" in worker, "只接受同源请求", "没有同源检查")
    check("worker:body_limit", "MAX_BODY_BYTES" in worker, "限制请求体大小", "没有请求体上限")
    check("worker:tool_allowlist", "TOOL_NAMES" in worker, "工具名单固定", "没有工具白名单")

    headers = _read(root, "site/src/_headers")
    csp = next((line for line in headers.splitlines() if "Content-Security-Policy" in line), "")
    script_src = re.search(r"script-src ([^;]+)", csp)
    check(
        "headers:csp_scripts",
        bool(script_src) and "'unsafe-inline'" not in script_src.group(1),
        "脚本只允许同源与 WebAssembly",
        "CSP 缺失或允许内联脚本",
    )

    dockerfile = _read(root, "Dockerfile")
    check(
        "docker:non_root",
        re.search(r"^USER\s+(?!root)\w+", dockerfile, re.MULTILINE) is not None,
        "非 root 运行",
        "镜像以 root 运行",
    )
    check("docker:healthcheck", "HEALTHCHECK" in dockerfile, "有健康检查", "缺少 HEALTHCHECK")
    dockerignore = _read(root, ".dockerignore")
    check(
        "dockerignore:secrets",
        ".env" in dockerignore and "secrets.toml" in dockerignore,
        "密钥不进镜像",
        ".dockerignore 未排除密钥文件",
    )
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    args = parser.parse_args()
    results = run_checks()
    if args.json:
        print(json.dumps([asdict(r) for r in results], ensure_ascii=False, indent=2))
    else:
        for result in results:
            print(f"[{'PASS' if result.ok else 'FAIL'}] {result.name}：{result.detail}")
    return 0 if all(r.ok for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
