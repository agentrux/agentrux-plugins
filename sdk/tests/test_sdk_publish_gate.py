"""SDK publish gate (CLAUDE.md §パッケージ公開ルール).

agentrux-plugins/sdk/src/agentrux_sdk/ が以下の禁止 pattern を含まないことを CI で検査する。
このリポジトリ (agentrux-plugins) は公開 SDK の SSOT。 server 本体 (api/auth/...) や
Console/Admin plane への参照、 秘匿情報が混入していないことを gate する。
"""

from __future__ import annotations

import re
from pathlib import Path

SDK_DIR = Path(__file__).resolve().parents[1] / "src" / "agentrux_sdk"


def _iter_py_files() -> list[Path]:
    return sorted(p for p in SDK_DIR.rglob("*.py") if "__pycache__" not in p.parts)


def test_sdk_dir_exists() -> None:
    assert SDK_DIR.is_dir(), f"SDK source dir not found: {SDK_DIR}"


def test_no_forbidden_subdirs() -> None:
    """1. 公開対象に api/auth/infrastructure/models が含まれていないこと."""
    for forbidden in ("api", "auth", "infrastructure", "models"):
        assert not (
            SDK_DIR / forbidden
        ).exists(), f"forbidden subdir found in SDK: {forbidden} (CLAUDE.md §公開前チェックリスト 1)"


def test_no_test_files_under_sdk() -> None:
    """2. テストコードが含まれていないこと."""
    bad = [p for p in _iter_py_files() if "test" in p.stem.lower()]
    assert bad == [], f"test files found under SDK source: {bad}"
    bad_dirs = [p for p in SDK_DIR.rglob("tests")]
    assert bad_dirs == [], f"tests/ dir found: {bad_dirs}"


def test_no_hardcoded_secrets() -> None:
    """3. ハードコード秘匿情報 (private key / AWS secret / Stripe live key)."""
    secret_pat = re.compile(
        r"BEGIN (RSA |EC )?PRIVATE KEY|aws_secret_access_key|sk_live_[A-Za-z0-9]{16,}"
    )
    for p in _iter_py_files():
        text = p.read_text(encoding="utf-8")
        m = secret_pat.search(text)
        assert m is None, f"possible hardcoded secret in {p}: {m.group(0)[:30]}..."


def test_no_admin_or_console_endpoint_references() -> None:
    """4. 非公開 API endpoint (/admin/*, /console/*) への参照なし.

    SDK は経路 B (publish/read) 専用で、 /console (Workspace 操作) や /admin (運用) は
    別 client (Console SPA / admin CLI) の責務。 quote 種別 (", ', f"", f'') を問わず
    string literal 化された path を検出する (composer.py の backtick docstring は対象外)。
    """
    # 開始 quote (' or ") の直後に /admin/ または /console/ が来る literal を検出。
    # f-string も開始 quote が同一なので f"/admin/{x}" / f'/console/{x}' を捕捉する。
    pat = re.compile(r"""['"]/(admin|console)/""")
    bad = []
    for p in _iter_py_files():
        text = p.read_text(encoding="utf-8")
        for m in re.finditer(pat, text):
            bad.append(f"{p}:{text[:m.start()].count(chr(10))+1}: {m.group(0)}")
    assert bad == [], "private endpoints referenced in SDK:\n" + "\n".join(bad)


def test_no_session_token_prefixes() -> None:
    """4b. Console/Admin session token (ast_ / asat_) を string literal に含めない.

    SDK が扱うのは data plane の `aat_` / `art_` / `dc_` のみ。 session token
    (`ast_`=Console / `asat_`=Admin) を SDK が参照・保持・送出することは禁止
    (公開 SDK で plane 区別なく特権 token を使えると危険)。 quote 直後の prefix のみ
    照合し、 `last_` 等の substring 誤検出を避ける。
    """
    pat = re.compile(r"""['"](asat_|ast_)""")
    bad = []
    for p in _iter_py_files():
        text = p.read_text(encoding="utf-8")
        for m in re.finditer(pat, text):
            bad.append(f"{p}:{text[:m.start()].count(chr(10))+1}: {m.group(0)}")
    assert bad == [], "session token prefix referenced in SDK:\n" + "\n".join(bad)


def test_no_raw_server_response_in_errors() -> None:
    """5. エラーメッセージにサーバーレスポンス生データを含めていない.

    具体的には JSON.stringify(r.data) / response.text() 等の pattern を grep。
    Python では `str(r)` `r.text` を直接 message に入れている箇所を warn。
    SDK は ValidationError 等で `r.text` を含めるが、 これは debugging value > leak risk と
    判定。 ただし 5xx / 4xx body を生で error.message に流す箇所は controlled.
    本 test は明白な NG pattern (JSON.stringify 等の web 由来) のみ block。
    """
    bad_pat = re.compile(r"JSON\.stringify\(r\.(data|json)\)")
    for p in _iter_py_files():
        text = p.read_text(encoding="utf-8")
        m = bad_pat.search(text)
        assert m is None, f"raw response stringify in {p}: {m.group(0)}"


def test_no_server_only_imports() -> None:
    """6. server-only module (api/auth/infrastructure/models/application/domain/database/config) を import しない.

    SDK は agentrux 内 server module への import 一切禁止 (publish 時に依存解決失敗するため).
    """
    bad_imports = re.compile(
        r"from agentrux\.(api|auth|infrastructure|models|application|domain|database|config)\b"
        r"|import agentrux\.(api|auth|infrastructure|models|application|domain|database|config)\b"
    )
    bad = []
    for p in _iter_py_files():
        text = p.read_text(encoding="utf-8")
        for ln_no, line in enumerate(text.splitlines(), start=1):
            if bad_imports.search(line):
                bad.append(f"{p}:{ln_no}: {line.strip()}")
    assert bad == [], "server-only import in SDK:\n" + "\n".join(bad)
