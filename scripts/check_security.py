"""提交前的敏感信息扫描：只看 Git 跟踪的文件，发现问题时退出码为 1。

    python scripts/check_security.py

检查项：公网 IP、私钥、常见云厂商与模型服务的 Key 格式、写死在代码或文档里的口令，
以及 MySQL 导出里的 DEFINER（会带出数据库账号和主机）。
示例值（your-…、change-me、<…>、测试里的 secret）不算问题；确需保留的行在行尾写 `security: allow`。
"""

from __future__ import annotations

import ipaddress
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEXT_SUFFIXES = {
    ".py", ".js", ".mjs", ".ts", ".json", ".jsonc", ".jsonl", ".md", ".txt", ".yaml", ".yml",
    ".toml", ".cfg", ".ini", ".env", ".example", ".sh", ".ps1", ".bat", ".html", ".css", ".sql", ".tsv",
}  # fmt: skip
SKIP_PREFIXES = ("site/dist/",)
ALLOW_MARK = "security: allow"
PLACEHOLDER = re.compile(
    r"^(|your[-_].*|change-me|changeme|secret|test|testing|example|dummy|xxx+|\*+|<.*>|\$\{.*\}|sk-test.*|none|null)$",
    re.IGNORECASE,
)

IPV4 = re.compile(r"(?<![\d.])(\d{1,3}(?:\.\d{1,3}){3})(?![\d.])")
PATTERNS = {
    "私钥": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"),
    "OpenAI 风格 Key": re.compile(r"\bsk-(?!your|test)(?:proj-)?[A-Za-z0-9_-]{24,}"),
    "AWS Access Key": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "GitHub Token": re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b"),
    "MySQL DEFINER": re.compile(r"DEFINER\s*=\s*`[^`]+`@`[^`]+`", re.IGNORECASE),
}
ASSIGNMENT = re.compile(
    r"""(?P<key>password|passwd|pwd|secret|api[_-]?key|access[_-]?token|auth[_-]?token)\w*
        \s*[:=]\s*["'](?P<value>[^"'\n]{4,})["']""",
    re.IGNORECASE | re.VERBOSE,
)


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    kind: str
    excerpt: str


def _public_ip(text: str) -> bool:
    try:
        address = ipaddress.ip_address(text)
    except ValueError:
        return False
    return address.is_global


def scan_text(path: str, text: str) -> list[Finding]:
    # 测试里传给被测代码的假口令是用例的一部分，只对测试之外的文件检查写死的口令
    check_assignments = not path.replace("\\", "/").startswith(("tests/", "legacy/tests/"))
    findings = []
    for number, line in enumerate(text.splitlines(), start=1):
        if ALLOW_MARK in line:
            continue
        for match in IPV4.finditer(line):
            if _public_ip(match.group(1)):
                findings.append(Finding(path, number, "公网 IP", match.group(1)))
        for kind, pattern in PATTERNS.items():
            if match := pattern.search(line):
                findings.append(Finding(path, number, kind, match.group(0)[:24]))
        for match in ASSIGNMENT.finditer(line) if check_assignments else ():
            value = match.group("value").strip()
            if not PLACEHOLDER.match(value) and not value.startswith(
                ("os.", "env.", "process.env")
            ):
                findings.append(
                    Finding(path, number, "写死的口令或密钥", f"{match.group('key')}=…")
                )
    return findings


def tracked_files(root: Path = ROOT) -> list[str]:
    output = subprocess.run(
        ["git", "ls-files"], cwd=root, check=True, capture_output=True, text=True, encoding="utf-8"
    ).stdout
    return [line for line in output.splitlines() if line]


def scan_repository(root: Path = ROOT) -> list[Finding]:
    findings = []
    for relative in tracked_files(root):
        if relative.startswith(SKIP_PREFIXES):
            continue
        path = root / relative
        if path.suffix.lower() not in TEXT_SUFFIXES and path.name not in {
            ".gitignore",
            "Dockerfile",
        }:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, FileNotFoundError):
            continue
        findings.extend(scan_text(relative, text))
    return findings


def main() -> int:
    findings = scan_repository()
    if not findings:
        print("[OK] 未发现敏感信息")
        return 0
    for finding in findings:
        print(f"[FAIL] {finding.path}:{finding.line} {finding.kind}：{finding.excerpt}")
    print(f"共 {len(findings)} 处，确认是示例值时在行尾加 `{ALLOW_MARK}`")
    return 1


if __name__ == "__main__":
    sys.exit(main())
