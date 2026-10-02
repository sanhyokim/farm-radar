"""N4b: 金庫の運用先の見張り（2026-10-03 オーナー: Hyperdrive の金庫は待ち時間なしで運用先を変えられる）。"""

from farm_radar.feeds import store, vaults

VAULT = "0x" + "ab" * 20
OTHER = "0x" + "cd" * 20
FACTORY = "0x" + "ef" * 20
KNOWN = {
    "hd": {"id": "hd", "name": "HD", "merkl_protocols": ["HD"], "match": {"base": [
        {"method": "addresses", "addresses": [VAULT], "watch": "morpho_vault_v2", "source_url": "u", "checked_at": "d"}]}},
    "mo": {"id": "mo", "name": "MO", "merkl_protocols": ["MO"], "match": {"base": [
        {"method": "factory", "factory": FACTORY, "function": "isVaultV2(address)", "watch": "morpho_vault_v2",
         "source_url": "u", "checked_at": "d"}]}},
}
CHAINS = {8453: {"id": "base"}}


class FakeRpc:
    def __init__(self, adapters):
        self.adapters = adapters

    def eth_call(self, to, data, block):
        sel = data[:10]
        word = lambda v: "0x" + format(v, "064x")                        # noqa: E731
        addr = lambda a: "0x" + a[2:].rjust(64, "0")                     # noqa: E731
        from farm_radar.rpc.abi import encode_call  # 関数の番号（先頭4バイト）を同じ方法で作る

        def s(sig, *t):
            return "0x" + encode_call(sig, t, tuple(0 for _ in t)).hex()[:8]
        if sel == s("adaptersLength()"):
            return word(len(self.adapters))
        if sel == s("adapters(uint256)", "uint256"):
            return addr(self.adapters[int(data[10:], 16)])
        return addr("0x" + "11" * 20)


def test_targets_are_listed_or_factory_verified_vaults(tmp_path):
    conn = store.connect(tmp_path / "f.sqlite3")
    assert [t["address"] for t in vaults.targets(conn, CHAINS, KNOWN)] == [VAULT.lower()]
    conn.execute("INSERT INTO venue_checks VALUES (8453, ?, ?, 'mo', 'isVaultV2(address)', 't', 1, NULL)",
                 (OTHER, FACTORY))
    conn.execute("INSERT INTO venue_checks VALUES (8453, ?, ?, 'mo', 'isVaultV2(address)', 't', 0, NULL)",
                 ("0x" + "99" * 20, FACTORY))
    assert sorted(t["address"] for t in vaults.targets(conn, CHAINS, KNOWN)) == sorted([VAULT.lower(), OTHER.lower()])


def test_a_change_in_adapters_is_recorded_and_found_for_a_practice_period(tmp_path, monkeypatch):
    monkeypatch.setattr("farm_radar.feeds.receipts.CALL_INTERVAL_SECONDS", 0)
    conn = store.connect(tmp_path / "f.sqlite3")
    a1, a2 = "0x" + "21" * 20, "0x" + "22" * 20
    first = vaults.read(conn, CHAINS, {"hd": KNOWN["hd"]}, rpc_factory=lambda c: FakeRpc([a1]))
    store.write_vault_states(conn, "2026-10-03T01:00:00+00:00", first)
    same = vaults.read(conn, CHAINS, {"hd": KNOWN["hd"]}, rpc_factory=lambda c: FakeRpc([a1]))
    store.write_vault_states(conn, "2026-10-03T02:00:00+00:00", same)
    assert vaults.changes_between(conn, 8453, VAULT, "2026-10-03T00:00:00+00:00") == []
    moved = vaults.read(conn, CHAINS, {"hd": KNOWN["hd"]}, rpc_factory=lambda c: FakeRpc([a1, a2]))
    store.write_vault_states(conn, "2026-10-03T03:00:00+00:00", moved)
    ch = vaults.changes_between(conn, 8453, VAULT, "2026-10-03T00:00:00+00:00", "2026-10-03T04:00:00+00:00")
    assert len(ch) == 1 and ch[0]["before"]["adapters"] == [a1] and sorted(ch[0]["after"]["adapters"]) == [a1, a2]
    assert vaults.changes_between(conn, 8453, VAULT, "2026-10-03T03:30:00+00:00") == []   # 入る前の変化は数えない
