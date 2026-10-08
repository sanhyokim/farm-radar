"""本物のお金につながるコードがないかを調べる（N6 は 100% 仮想。CLAUDE.md の絶対ルール1・2）。

N6 のファイル（farm_radar/n6/*.py）に、送金・署名・秘密鍵・ウォレットの接続を扱う言葉がないかを見る。
この調べ方そのものが言葉を含むので、n6 の外に置く。試験と、N6 の異常チェック（n6/report.py）の両方で使う。
"""

from __future__ import annotations

import re
from pathlib import Path

FORBIDDEN = re.compile(r"private_?key|mnemonic|seed phrase|sign_transaction|send_raw|sendRawTransaction|eth_sendTransaction|"
                       r"web3\.eth\.account|Account\.from_key|LiveExecutor", re.I)

N6_DIR = Path(__file__).parent / "n6"


def scan(root: Path = N6_DIR) -> list[str]:
    """見つかったファイルと言葉（なければ空）。"""
    out = []
    for p in sorted(root.glob("*.py")):
        m = FORBIDDEN.search(p.read_text(encoding="utf-8"))
        if m:
            out.append(f"{p.name}: {m.group(0)}")
    return out
