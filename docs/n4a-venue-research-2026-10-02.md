# Venue ownership checks for Merkl opportunities (Base 8453 / Robinhood Chain 4663)

Checked on: **2026-10-02** (all sources and all on-chain reads below were checked on this date)
Scope: read-only research. No transactions were sent, nothing was signed, and no keys were used. Every on-chain check was an `eth_call`, `eth_getCode`, or `eth_getStorageAt`.
RPCs: Base `https://mainnet.base.org` (block ~52,081,300–52,081,700). Robinhood `https://rpc.mainnet.chain.robinhood.com` (from `chains/robinhood.yaml`, block ~78,309,100–78,314,600).
Scratch logs with the raw call output are in this folder: `morpho_calls.log`, `hyperdrive_calls.log`, `hyperdrive_gov.log`, `vaultv2_gov.log`, `ipor_calls.log`, `ys_calls.log`, `zia_calls.log`, `uniswap_calls.log`, `morpho_market_calls.log`. The helper is `ethcall.py`, which is read-only `eth_call` with backoff.

## 日本語の要約（オーナー向け）
- **Morpho**: Merkl の預け先の住所が Morpho の正式な金庫かどうかは、Morpho 公式の「工場」コントラクトに `isVaultV2(住所)` または `isMetaMorpho(住所)` を聞けば true/false で分かります。Base の15件と Robinhood の1件（Steakhouse USDG V2）は、すべて true でした。Robinhood には Morpho の公式な展開があります（公式アドレス一覧に記載あり）。
- **Hyperdrive**: Merkl の「Hyperdrive USDC Ecosystem」は、実は **Morpho Vault V2 の金庫**です（`isVaultV2` が true）。運営は hyperdrive.fi / hyperdrive.finance（X: @hyperdrivedefi、Ambit Labs）です。公式ドキュメントに、この金庫の住所とその管理者が書かれています。Merkl が付けている DefiLlama の名前 `hyperdrive` は、**別の会社（DELV）の終了したプロジェクト**なので誤りです。正しくは `hyperdrive-hl-lending` / `hyperdrive-hl-earn`（ただし Base は数えられていない）。注意点として、この金庫の待ち時間（タイムロック）はすべて0秒です。
- **IPOR Fusion**: 工場の `isFusionVault(住所)` は、最近作られた金庫にしか効きません。Merkl に載っている金庫は全部古いので false が返ります。代わりに、公式の住所一覧（GitHub の ipor-abi）、または「公式の型（実装）を指す複製かどうか」で確かめます。19件すべてが公式一覧にありました。
- **Yieldseeker**: 金庫型ではなく、利用者ごとの「エージェント財布」です。Merkl の住所（0xDCff…）はコントラクトではありません。確認は財布 → `owner()` と `ownerAgentIndex()` → 工場の `userWallets(owner, idx)` が同じ財布を返すか、で行います。
- **Zia のフック（0x64e9ae10…）**: Zia（旧 TradeGPT、zia.finance）の `ZiaFeeHook` でした（Sourcify で中身を確認済み）。持ち主もアップグレードもありません。手数料は上限付きで、1つの個人の財布（EOA）が変更できます。
- **Uniswap**: v4 の PoolManager は作り替えられません。管理者ができるのは手数料関係だけで、その管理者は Base でも Robinhood でも Uniswap ガバナンス（Ethereum 上のタイムロック、2日待ち）です。バグ報奨金は Cantina で最大 $15.5M です。

---

## 1. Morpho

### 1.1 How to tell a vault is a real Morpho vault
Morpho's official factories record every vault they create in a public mapping, so a single view call answers yes or no:

| Vault type | Factory function (exact) | Source code |
|---|---|---|
| Vault V2 | `isVaultV2(address) returns (bool)`, selector `0x5edec50d` | `mapping(address account => bool) public isVaultV2;` in https://github.com/morpho-org/vault-v2/blob/main/src/VaultV2Factory.sol and `function isVaultV2(address account) external view returns (bool);` in `src/interfaces/IVaultV2Factory.sol`. Checked at commit 30ca75f6 (2026-10-02). |
| Vault V1.1 (MetaMorpho v1.1) | `isMetaMorpho(address) returns (bool)`, selector `0x29b5352c` | `mapping(address => bool) public isMetaMorpho;` in https://github.com/morpho-org/metamorpho-v1.1/blob/main/src/MetaMorphoV1_1Factory.sol |
| Vault V1 (old MetaMorpho) | `isMetaMorpho(address) returns (bool)` | `mapping(address => bool) public isMetaMorpho;` in https://github.com/morpho-org/metamorpho/blob/main/src/MetaMorphoFactory.sol |

`VaultV2Factory.createVaultV2` sets `isVaultV2[newVaultV2] = true` when it deploys a vault with `new VaultV2{salt}`. Vaults are not proxies.
For Merkl market opportunities (`MORPHOSUPPLY`, `MORPHOBORROW`, `MORPHOCOLLATERAL`), the explorer address is a token, not a vault. The full market id is in `depositUrl`. To check it, call `Morpho.idToMarketParams(bytes32)` on the official Morpho Blue contract. A non-zero result means the market exists.

### 1.2 Official addresses
Source for all rows: https://docs.morpho.org/developers/contracts/addresses/ (markdown at `https://docs.morpho.org/developers/contracts/addresses.md`). The old URL `/get-started/resources/addresses` redirects there. Checked 2026-10-02.

| Chain | Contract | Address | Extra verification |
|---|---|---|---|
| Base 8453 | VaultV2Factory | `0x4501125508079A99ebBebCE205DeC9593C2b5857` | base.blockscout.com: verified, name `VaultV2Factory` |
| Base 8453 | MetaMorpho Factory V1.1 | `0xFf62A7c278C62eD665133147129245053Bbf5918` | Blockscout: fully verified `MetaMorphoV1_1Factory` |
| Base 8453 | MetaMorpho Factory [OLD] | `0xA9c3D3a366466Fa809d1Ae982Fb2c46E5fC41101` | Blockscout: fully verified `MetaMorphoFactory` |
| Base 8453 | MorphoRegistry (adapter registry) | `0x5C2531Cbd2cf112Cf687da3Cd536708aDd7DB10a` | (docs) |
| Base 8453 | Morpho (Blue) | `0xBBBBBbbBBb9cC5e90e3b3Af64bdAF62C37EEFFCb` | (docs) |
| Robinhood 4663 | VaultV2Factory | `0x0FBad98595b0186dA120E41f77C102beb49f803c` | Sourcify 4663: `match` (partial), name `VaultV2Factory` |
| Robinhood 4663 | MorphoRegistry | `0xe785a2eFD384BA7B95BaEd3851BC76aeD67C676f` | (docs) |
| Robinhood 4663 | Morpho (Blue) | `0x9D53d5E3bd5E8d4Cbfa6DB1ca238AEA02E651010` | (docs) |
| Robinhood 4663 | Adaptive Curve IRM | `0x2BD3d5965B26B51814AC95127B2b80dD6CcC0fa1` | (docs) |

Notes:
- **Morpho has an official deployment on Robinhood Chain (4663).** The docs list Morpho Blue, Vault V2, Midnight, Bundles and Bundler3 for Robinhood.
- **There is no Vault V1 (MetaMorpho) factory on Robinhood.** The Vault V1 section's chain tabs do not include Robinhood, so Robinhood vaults must use the `isVaultV2` check.
- Morpho's docs link Robinhood addresses to `https://robin.etherscan.io/address/...`, an Etherscan explorer for Robinhood. Our `chains/robinhood.yaml` uses Blockscout. Both appear to exist; I did not verify robin.etherscan.io further.
- Robinhood Blockscout (`robinhoodchain.blockscout.com/api/v2/...`) returned a Cloudflare "Just a moment..." page from this environment, the same as noted in `venues/up-robinhood.yaml`. Sourcify worked.

### 1.3 Read-only eth_call verification (all done 2026-10-02)
The sample vaults are every Base Morpho vault opportunity in Merkl (`/v4/opportunities?chainId=8453&mainProtocolId=morpho&status=LIVE`) plus the Robinhood Steakhouse vault.

| Chain | Vault (Merkl name) | isVaultV2 | isMetaMorpho v1.1 | isMetaMorpho old |
|---|---|---|---|---|
| Base | 0xbeeff2490FEffa212faC2f6553682C219E6a8845 Steakhouse High Yield USDC V2 | **1** | 0 | 0 |
| Base | 0x91C056B6d4311a743614FBc03ac32d4E6A2d3a3c Clearstar cbAssets V2 | **1** | 0 | 0 |
| Base | 0x8B12106a70FE2F8bB255F691f1CF9a9ccfB278AF Noon Ecosystem V2 | **1** | 0 | 0 |
| Base | 0xBCA4E2E24A7cFa776E4282CC8Eb06f04738b71da Clearstar Reactor ETH V2 | **1** | 0 | 0 |
| Base | 0x392B3CCf36C8adB8094F45573346B457414cD752 KPK USDC Yield V2 | **1** | 0 | 0 |
| Base | 0xF6190F97f7AE540234f5924B8992b9C3FD8381b9 Jarvis USDC | 0 | **1** | 0 |
| Base | 0xc1256Ae5FF1cf2719D4937adb3bbCCab2E00A2Ca Moonwell Flagship USDC | 0 | 0 | **1** |
| Base | 0x08cc279532f159BdB450eBc0bAf6562f585Ded58 Bitso MXNB Prime V2 | **1** | 0 | 0 |
| Base | 0xa0E430870c4604CcfC7B38Ca7845B1FF653D0ff1 Moonwell Flagship ETH | 0 | 0 | **1** |
| Base | 0xf24608E0CCb972b0b0f4A6446a0BBf58c701a026 Moonwell Flagship EURC | 0 | 0 | **1** |
| Base | 0x543257eF2161176D7C8cD90BA65C2d4CaEF5a796 Moonwell Frontier cbBTC | 0 | 0 | **1** |
| Base | 0xdbA76Bc542bb07538e046B40F2e8a215B409F7A8 Moonwell Frontier cbBTC V2 | **1** | 0 | 0 |
| Base | 0x48a90E85be5C56b0A669985A12ee7C449fC79965 Moonwell Flagship USDC V2 | **1** | 0 | 0 |
| Base | 0x89BeDBB1C4837444Da215A377275Ff96A84D6f53 Moonwell Flagship ETH V2 | **1** | 0 | 0 |
| Base | 0x9949CCaC462fA932565c968f2DBd21E0F43293b6 "unknown V2 vault" | **1** | 0 | 0 |
| Base | 0x58a0f600d555eb25b792Ec2c6824D0bff5127B1F Hyperdrive USDC Ecosystem (see §2) | **1** | 0 | 0 |
| Robinhood | 0xBeEff033F34C046626B8D0A041844C5d1A5409dd Steakhouse USDG V2 | **1** (VaultV2Factory 0x0FBa…803c) | n/a | n/a |
| Base (negative control) | USDC 0x8335…2913 | 0 | 0 | — |

Every Merkl Morpho vault on Base and Robinhood is a genuine Morpho vault. Each one is recognized by exactly one factory, so the checker must try all three factories on Base and only V2 on Robinhood.
Market checks (`idToMarketParams`): Base market `0x54cf9be5…7354` returned USDC/USDe with LLTV 0.915. Robinhood market `0xc845da65…ddd6` returned loan `0x5fc5…d168` / collateral USDe `0x5d3a…ef34` with LLTV 0.915, using IRM `0x2BD3…0fa1` (the official Robinhood IRM). Both are real markets.

### 1.4 Admin / ownership
- Core: "Core contracts are immutable" (https://docs.morpho.org/learn/resources/risks, checked 2026-10-02). Morpho Blue is "implemented as an immutable smart contract".
- Vault V2: there are no proxies. The roles are owner, curator, allocators and sentinels. The owner "cannot do actions that can directly hurt depositors" but can set the curator and sentinels. The curator's changes are timelocked **per function**. **The default timelock is 0** and is set by each vault's curator. `abdicate(selector)` disables a function forever. Sources: NatSpec in `vault-v2/src/VaultV2.sol` (lines ~100–148) and https://docs.morpho.org/curate/concepts/timelock (checked 2026-10-02).
- Vault V1 / V1.1: one global timelock. After setup it must be between 1 day and 2 weeks (same timelock page).
- Timelocks therefore differ per vault and must be read per vault: `timelock(bytes4 selector)`, `abdicated(bytes4)`, `owner()`, `curator()`, `adapterRegistry()`. Readings on 2026-10-02:
  - Robinhood Steakhouse USDG V2: owner `0xca50…db73` (contract, 8.6 KB, not a Safe or OZ timelock; type unconfirmed), curator `0x9023…42fb` (Safe, threshold 3). `adapterRegistry` is the official Robinhood MorphoRegistry `0xe785…676f`, and `setAdapterRegistry` is **abdicated**. addAdapter, increaseAbsolute/RelativeCap, removeAdapter and increaseTimelock are each 7 days. setIsAllocator and fees are 0.
  - Base Steakhouse High Yield USDC V2: registry = official MorphoRegistry `0x5C25…B10a` (abdicated). addAdapter and caps are 3 days. increaseTimelock and removeAdapter are 7 days.
  - Base Hyperdrive USDC Ecosystem: **every timelock is 0, there is no adapter registry, and nothing is abdicated** (see §2).
- Useful rule: a V2 vault whose `adapterRegistry()` is the official MorphoRegistry with `abdicated(setAdapterRegistry)` = true can only ever use Morpho-approved adapters.

### 1.5 DefiLlama, hacks, bug bounty
- Slugs: `morpho-blue` (name "Morpho Blue", url https://app.morpho.org, twitter `Morpho`, parent `parent#morpho`; chains include Base and **Robinhood Chain**). There is also `morpho-midnight` (Base and Ethereum only). Merkl's own trustData also says `morpho-blue`. Source: https://api.llama.fi/protocols (2026-10-02).
- Hacks (https://api.llama.fi/hacks; public, no key, HTTP 200, 1,293 records): no entry with defillamaId 4025 (morpho-blue). There is one related entry: "LeadBlock's Morpho Blue Market", 2024-10-14, $250k, Oracle Misconfiguration, Ethereum, defillamaId null. That was a curator market oracle problem, not a core bug.
- Bug bounty: **Cantina, $2,500,000**, covering "Morpho Blue, Morpho Midnight & Morpho Vaults". URL https://cantina.xyz/bounties/35a5f0a1-2ffd-432c-8f3b-77d169add8c3, quoted on https://docs.morpho.org/learn/resources/risks (checked 2026-10-02). Immunefi `/bug-bounty/morpho/` returns 404.

---

## 2. Hyperdrive ("Supply to the Hyperdrive USDC Ecosystem vault", Base)

### 2.1 Which Hyperdrive this is
- Merkl opportunity: chain 8453, protocol id `hyperdrive-lending`, explorer address `0x58a0f600d555eb25b792Ec2c6824D0bff5127B1F`, depositUrl `https://v2.hyperdrive.finance/vaults/base/usdc`, Merkl protocol url `https://app.hyperdrive.fi/earn`.
- On-chain (2026-10-02): `name()` = "Hyperdrive USDC Ecosystem", `symbol()` = HDV-USDC-ECO, `asset()` = USDC. **`VaultV2Factory(0x4501…5857).isVaultV2(vault)` = true**, so this is a **Morpho Vault V2** curated by Hyperdrive. Blockscout lists it as a verified `VaultV2`.
- The official project is **Hyperdrive by Ambit Labs** (X `@hyperdrivedefi`):
  - https://hyperdrive.finance/ links to docs.hyperdrive.finance, `github.com/ambitlabsxyz/hyperdrive-audits`, `x.com/hyperdrivedefi` and v2.hyperdrive.finance.
  - https://hyperdrive.fi/ links to `x.com/hyperdrivedefi` and the gitbook `hyperdrive-2.gitbook.io/hyperdrive` (v1 on HyperEVM).
- **Official confirmation of the vault address:** https://docs.hyperdrive.finance/developers/contracts/ (checked 2026-10-02), section "Base (chain id 8453) → USDC Vault":
  - Vault `0x58a0f600d555eb25b792Ec2c6824D0bff5127B1F`; Owner = Governance timelock; Curator = Safe; Sentinel `0x4aF56dC525bAf74285Ae588562eF8390815F265b`; Allocator `0x374b5e6b92f3D9d175a1Af1B39151b48DCaE2111`
  - Base governance: Safe (multisig) `0x63Dfa1Ad0f271A4b6EcBAe0136c78376a82657D7`; Governance timelock `0x54DB0C5C717681475FCb7bF94769888CFcF8829E`. The timelock's proposers are the Safe **and the deployer EOA `0x6B887caC2e0Ef29306E1B6F22E716796105137a0`**.
  - On-chain match: `owner()` = 0x54db…829e, `curator()` = 0x63df…57d7, `isSentinel(0x4aF5…265b)` = true, `isAllocator(0x374b…2111)` = true. All four match the docs.
- The old gitbook page (`hyperdrive-2.gitbook.io/hyperdrive/for-developers/contract-addresses`) lists only HyperEVM contracts. It has no Base vault.
- **Unrelated project with the same name:** DELV's "Hyperdrive" (fixed-rate AMM, github delvtech/hyperdrive). It has nothing to do with this vault.

### 2.2 Check method
Use the Morpho `VaultV2Factory.isVaultV2(address)` check (§1), plus an allowlist from Hyperdrive's contracts page (vault address and expected owner/curator). There is no Hyperdrive-specific factory for Base vaults.

### 2.3 Admin / ownership (2026-10-02, on-chain)
- Governance timelock `0x54DB…829E`: **`getMinDelay()` = 0 seconds.** The docs say every timelock is self-administered.
- Curator Safe `0x63Df…57D7`: Safe v1.4.1, **threshold 3 of 5 owners**.
- Vault timelocks: **0 for every curator function checked** (addAdapter, setAdapterRegistry, increaseAbsoluteCap/RelativeCap, setIsAllocator, gates, fees, increaseTimelock, removeAdapter). `adapterRegistry()` = 0x0, so there is no restriction to Morpho-approved adapters. Nothing is abdicated. There are 3 adapters:
  - `0x87bc…351f`, the liquidity adapter
  - `0x5b1f…fe01`
  - `0x6fcd…b861`

  As a result, the curator multisig, or the owner through a 0-delay timelock, can change the strategy instantly. Depositors get no warning period. **This is the main risk flag for this venue.**

### 2.4 DefiLlama, hacks, bug bounty
- **Correct slugs:** `hyperdrive-hl-lending` (url https://hyperdrive.fi/, twitter `hyperdrivedefi`, parent `parent#hyperdrive-hl`, chains Hyperliquid L1 only) and `hyperdrive-hl-earn` (same url and twitter, Hyperliquid L1). **Neither tracks Base**, so this Base vault is not counted under Hyperdrive on DefiLlama.
- **Wrong slug:** Merkl's trustData for `hyperdrive-lending` says slug `hyperdrive`. That DefiLlama entry is **DELV's** Hyperdrive (twitter `HyperVueFDN`, parent `parent#delv`, `deadUrl`, audit links `github.com/delvtech/hyperdrive`). Do not use it.
- Hacks: **"Hyperdrive HL Lending", 2025-09-27, $782,000, Access Control / Arbitrary External Call, Hyperliquid L1** (defillamaId 6298, parent `parent#hyperdrive-hl`). Source https://api.llama.fi/hacks (2026-10-02). The hack was on the earlier HyperEVM product, not this Base vault, but it is the same team.
- Bug bounty: **none found.** Immunefi slugs hyperdrive, hyperdrivedefi, hyperdrive-hl, ambit and ambitlabs all return 404. Neither docs.hyperdrive.finance nor the gitbook mentions a bounty. Audits: Bailsec, September 2026, RWA markets (https://docs.hyperdrive.finance/developers/audits/). The gitbook lists Kiki, Bailsec, Obsidian and Enigma Dark (2025) for the v1 HyperEVM contracts.

---

## 3. IPOR Fusion (Base Plasma/Fusion vaults)

### 3.1 Check method
- The FusionFactory has `isFusionVault(address) returns (bool)`, selector `0x233f3452`. Source: https://github.com/IPOR-Labs/ipor-fusion/blob/main/contracts/factory/FusionFactory.sol (line ~378) and `contracts/safe-harbor/IFusionFactoryVaultCheck.sol` (commit 632350d6, 2026-09-17). The source NatSpec says: "`isFusionVault` is introduced by IL-8227. **Factory deployments predating that version do not expose the selector; callers must treat a reverting call as 'not a Fusion vault' and rely on a manual allowlist instead.**" The mapping is filled only inside `clone()`, so older vaults were never back-filled.
- On Base, the factory proxy implements the selector (it is present in the bytecode of implementation `0x378fEb2E…4014`). But **it returns false for every Merkl IPOR vault**, and true only for the 4 newest vaults created after the upgrade. Verified: `deltaneutralHYPE` `0xfd71…3f9C`, `RWA hub Vault 1` `0x50Cc…7cbf`, `Optimizer Vault Usdc` `0x54EE…9550` and `28.09.2026` `0x4F1A…5272` all return 1. All 19 Merkl vaults return 0.
- **Recommended check, in order:**
  1. `FusionFactory.isFusionVault(v)` == true, which works for new vaults.
  2. Otherwise, check whether `v` is in the official registry `IPOR-Labs/ipor-abi` → `mainnet/addresses.json` → `base.vaults[].PlasmaVault`. That file has 273 Base vaults and was last updated 2026-09-28 at commit 26d5ad93. This list includes test vaults, so being listed means "created with IPOR's tooling", not endorsement.
  3. As an extra signal, `eth_getCode(v)` may be an EIP-1167 minimal proxy pointing to `IporFusionPlasmaVaultCoreBase` `0x7924184350e9d555a576153Fe5fEEEC86270530a`, which is listed in `mainnet/mainnet-base-fusion/addresses.json`. Older vaults are full deployments instead.
  4. Optional "DAO-reviewed" signal: the vault appears in the public listing API `https://api.ipor.io/dapp/plasma-vaults-list`. That API is documented at https://docs.ipor.io/build-on-fusion/developer-guide/api and needs no key. It has 95 vaults, 33 of them on Base, each with a `curator` field. Listing rules are at https://docs.ipor.io/build-on-fusion/atomists/curating-a-fusion-vault/public-vault-listing.

### 3.2 Official addresses
| Chain | Contract | Address | Source |
|---|---|---|---|
| Base 8453 | IporFusionFactoryProxy | `0x1455717668fA96534f675856347A973fA907e922` | https://docs.ipor.io/build-on-fusion/developer-guide/addresses and https://github.com/IPOR-Labs/ipor-abi/blob/main/mainnet/mainnet-base-fusion/addresses.json |
| Base 8453 | IporFusionFactoryImpl | `0x378fEb2E96082FB965E2BB922AEE484d07c14014` | ipor-abi (above). On-chain EIP-1967 slot = this address. **The docs table still shows the older `0x610152A7…33b7`, so the docs are stale. The docs themselves say ipor-abi is the source of truth.** |
| Base 8453 | IporFusionPlasmaVaultFactory | `0x71214DD40Cc3feAC5F355140c1A53776c1973B3B` | ipor-abi |
| Base 8453 | IporFusionPlasmaVaultCoreBase (clone target) | `0x7924184350e9d555a576153Fe5fEEEC86270530a` | ipor-abi |
| Robinhood 4663 | IporFusionFactoryProxy | `0xDF7A590b60072476E355A8EfF33e4872440bDc7C` | https://github.com/IPOR-Labs/ipor-abi/blob/main/mainnet/mainnet-robinhood-fusion/addresses.json. It is not in the docs table, and `mainnet/addresses.json` → `robinhood` has **no vaults** yet. I did not check it on-chain. |

On-chain (Base): `getFusionFactoryVersion()` = 9 and `getFusionFactoryIndex()` = 269.

### 3.3 eth_call results for the Merkl Base IPOR vaults (2026-10-02)
All 19 are in ipor-abi `base.vaults`, and all return `isFusionVault` = 0.
- EIP-1167 clones of the official CoreBase `0x7924…530a` (12): TESS cbETH Loop `0x3e21…93bF`, Base USDC LO `0xD46a…0F37`, TAU cbETH `0xe883…4CC8`, Base cbETH Loooper `0x5900…DBc1`, Meta/Apple/Google/Nvidia/Microsoft/Coinbase Carry Trade, TAU Base USDC LO, Base ETH LO, TAU Base ETH LO.
- Full-code deployments (older, 7): Autopilot wETH/USDC (Harvest), yoUSD/yoETH Loooper, IPOR wstETH Base, IPOR USDC Lending Optimizer Base.
- In IPOR's DAO-reviewed public list: 18 of 19. **Not listed:** "TESS cbETH (Base) Loop Vault" `0x3e212F136244465d8E524Fb658E6Daa1fb0593bF` (Merkl TVL about $4.8M). It is a genuine factory clone, but IPOR's frontend does not list it.
- Each vault has its own `authority()` (IporFusionAccessManager). For example, IPOR USDC LO Base uses `0x051f…8a81`.

### 3.4 Admin / ownership
- Each vault is administered by its own "Atomist" (curator) through an OpenZeppelin AccessManager with roles OWNER, ATOMIST, ALPHA and GUARDIAN. Execution delays (timelocks) are set **per vault by the owner**, and IPOR only recommends them. Sources: https://docs.ipor.io/build-on-fusion/atomists/vault-configuration-step-by-step/timelocks-and-execution-delays and the "Vault Governance & Depositor-Led Security Best Practices" page (listed in https://docs.ipor.io/llms.txt).
- The FusionFactory itself is a UUPS proxy, upgradeable by `DEFAULT_ADMIN_ROLE` (`_authorizeUpgrade`, FusionFactory.sol line ~440). Each vault's delays must be read from its AccessManager. I did not read them per vault.
- Curators of the Merkl vaults, per the IPOR API: IPOR DAO, Clearstar, TAU, Harvest (Autopilot), and Tesseract/TESS.

### 3.5 DefiLlama, hacks, bug bounty
- Slug: **`fusion-by-ipor`** (name "Fusion by IPOR", url https://app.ipor.io/fusion, which matches the Merkl protocol url; twitter `ipor_io`; parent `parent#ipor`; chains include Base). The related `ipor-derivatives` is a different product. Merkl has no trustData for IPOR.
- Hacks: **"Fusion by IPOR", 2026-01-06, $336,000, Access Control / Arbitrary External Call, Arbitrum** (defillamaId 5145). Source https://api.llama.fi/hacks (2026-10-02).
- Bug bounty: Immunefi https://immunefi.com/bug-bounty/ipor/information/ (updated 2026-09-24). The page shows **max $1,000** (critical, flat), while the program text says payouts are "capped at $100K". The scope is 12 Ethereum contracts from IPOR's derivatives product; **Fusion and PlasmaVault are not in scope.** In short, there is no meaningful bounty for Fusion vaults on Base. IPOR docs (security-and-audits) do not mention a bounty.

---

## 4. Yieldseeker (Base)

### 4.1 What Merkl lists
Opportunity type `ENCOMPASSING`, `explorerAddress` = null, identifier `0xDCff65f39E3b47f860e4E8A6dEc57c912b66c5CE`. On-chain, **that address has no code** (it is an EOA), so it is not a deposit contract. Users deposit into their own **AgentWallet**, a per-user ERC-1967/UUPS proxy that the factory creates through an operator role. There is no shared vault address, so an "explorer_address belongs to venue" check does not apply directly.

### 4.2 Check method and addresses
- Official addresses: https://github.com/tokenpage/yieldseeker-contracts/blob/main/deployments.json (commit 96f9dddf, 2026-09-27). The docs call this file "the source of truth": https://docs.yieldseeker.xyz/llms-full.txt, section "Governance, registries, pauses, and timelocks". Chain: Base. Addresses:
  - agentWalletFactory `0x9c7410a0faCAC60850C46eE5b58B518daec95130`
  - adminTimelock `0x8E074B7636F6A56097F1f719e708E2C932E23bAB`
  - adapterRegistry `0x4A5c3Cca0Ac2a1949D600891A89dd19B35189Cf0`
  - agentWalletImplementation `0x613Db9c924ADB7A7A90c11b02Cce20c913E3Fe50`
  - feeTracker `0x26f4Bb36dbb42fD38956c6c1E6602A9182C957DB`
- On-chain linkage (2026-10-02):
  - `factory.agentWalletImplementation()` = 0x613d…fe50 and `factory.adapterRegistry()` = 0x4a5c…9cf0. Both match the file.
  - `factory.hasRole(DEFAULT_ADMIN_ROLE, adminTimelock)` = true, and it is the only admin (role member count 1).
- Wallet check (the factory has no `isWallet` mapping): call `w.owner()` and `w.ownerAgentIndex()`, then `factory.userWallets(owner, idx)`. If the result equals `w`, the wallet belongs to Yieldseeker. Source: `src/agentwalletkit/AWKAgentWalletFactory.sol` (`mapping(address => mapping(uint256 => address)) public userWallets`).
  - Verified: sample wallet `0x24c1031fa0c35793c41023fca522471a369c8611`, taken from a factory `AgentWalletCreated` log at block 52,048,444 via base.blockscout.com. Its owner is `0x7388…8521` and its idx is 0. `userWallets(owner,0)` returned the same wallet, so it **MATCHES**. Negative control `userWallets(owner,57)` returned 0x0.
  - Note: the public Base RPC rejected `eth_getLogs` with HTTP 413. Blockscout's API worked.

### 4.3 Admin / ownership
AdminTimelock `getMinDelay()` = **345,600 s (4 days)** on-chain, which matches the docs ("production ... four days"). The timelock is executed through a Safe (repo README). The factory admin can change the implementation only for new wallets. Existing wallets are upgraded only when the owner calls `upgradeToLatest`. An EMERGENCY_ROLE on the registry can pause or unregister adapters without a timelock. Source: docs (above) and the repo README.

### 4.4 DefiLlama, hacks, bug bounty
- Slug **`yieldseeker`** (url https://yieldseeker.xyz, twitter `yieldseekerxyz`, Base only). This matches the Merkl protocol url and Merkl's trustData slug.
- Hacks: none on DefiLlama (no entry for id 7610).
- Bug bounty: **none found.** Immunefi `/bug-bounty/yieldseeker/` returns 404, and the full docs have 0 mentions of "bounty". Audit: Nethermind (2026-03-05), https://github.com/tokenpage/yieldseeker-contracts/tree/main/audits.

---

## 5. Uniswap v4 hooks with prefix 0x64e9ae10 on Robinhood Chain ("Zia ... (Dynamic)")

### 5.1 Identification
- Merkl Robinhood `UNISWAP_V4` opportunities with protocol id `zia` (8 live). Their explorerAddress is the official PoolManager `0x8366…0951`, and their identifier is the v4 PoolId.
- For each PoolId I called the official PositionManager (`0x58da…4fA7`, from `chains/robinhood.yaml`) `poolKeys(bytes25)`, then recomputed `keccak256(abi.encode(PoolKey))`. **All 8 PoolIds match.** All 8 use **hooks `0x64e9ae1066c47ac4a3cc0a5bd7b135908590e088`** with dynamic fee (0x800000).
- Hook source: **Sourcify chain 4663, `match` (partial): `src/ZiaFeeHook.sol:ZiaFeeHook`, `@author Zia`**. Link: https://repo.sourcify.dev/4663/0x64e9ae1066c47ac4a3cc0a5bd7b135908590e088 (API: https://sourcify.dev/server/v2/contract/4663/0x64e9ae1066c47ac4a3cc0a5bd7b135908590e088). On-chain `poolManager()` = official PoolManager.
- Hook flags from the address bits: `0x2088` = beforeInitialize, beforeSwap and **beforeSwapReturnsDelta**. The last flag lets the hook take a fee out of the swap amount. Flag constants come from https://github.com/Uniswap/v4-core/blob/main/src/libraries/Hooks.sol.
- Official-site evidence:
  - Zia's own app bundle (https://app.zia.finance/assets/v4PoolRouting-Ch7bROX2.js, fetched 2026-10-02) lists these exact PoolIds with their Merkl campaign ids. It reads `poolConfigs(bytes32)` from the pool's `hooks` and calls it "Zia hook".
  - Docs https://docs.zia.finance/liquidity/fees-and-apr.html say: "Robinhood Chain pools configured with the Zia V4 hook ... LP fee up to 1.00% and the Zia hook fee up to 0.25%". This matches the contract constants `MAX_LP_FEE = 10_000` and `MAX_HOOK_FEE_BPS = 25`.
  - tradegpt.finance (the Merkl protocol url) redirects to zia.finance in the site's loader script. TradeGPT is Zia's former name.
- **The hook address itself is not published on an official addresses page.** https://docs.zia.finance/security/contracts.html lists only 0G Uniswap-v3 contracts. Treat the address as attributed, with evidence from Sourcify plus the app bundle plus the docs, but mark it `unverified: true` for "official address list".

### 5.2 Admin model (from the verified source and on-chain reads)
- From NatSpec: "The contract has **no owner, proxy, upgrade path, pause, or arbitrary fee recipient**." The EIP-1967 slot is 0.
- `keeper` (immutable) = `0xe329342efe03f928fe820c5039e1b0ac7889631b`, an **EOA** with no code. It can set each pool's LP fee (≤1.00%) and hook fee (≤0.25%) instantly, with no timelock.
- `treasury` (immutable) = `0x726aef283bc276a887c2db95519570cd17774e67`, also an **EOA**.

### 5.3 DefiLlama, hacks, bug bounty
- Slug **`zia`** (url https://zia.finance/, twitter `zia_finance`, category Dexs). **Its chains list only 0G; Robinhood Chain is not tracked.** Merkl's trustData also says `zia`.
- Hacks: none.
- Bug bounty: none found. Immunefi zia, zia-finance and tradegpt all return 404. Audit: Halborn, September 2025, "Uniswap v3" (0G). That audit does not cover the v4 hook. Source: https://docs.zia.finance/security/audits.html.

---

## 6. Uniswap (v3 and v4) on Base and Robinhood Chain

Core addresses are already in `chains/base.yaml` and `chains/robinhood.yaml`.

### 6.1 Bug bounty
- Official pointer: `SECURITY.md` in https://github.com/Uniswap/v4-core says "Bug bounty details can be found in https://uniswap.org/bug-bounty". That URL redirects to **https://cantina.xyz/bounties/f9df94db-c7b1-434b-bb06-d1360abdd1be** (checked 2026-10-02).
- Cantina program "Uniswap": **maximum reward $15,500,000** (Critical $15.5M / High $1M / Medium $100k), started 26 Nov 2024.
  - **v4 Core** critical: up to **$15,500,000**.
  - **v3-Core** (UniswapV3Factory, UniswapV3Pool): Critical **$2,250,000** / High $500,000 / Medium $50,000.
  - The scope does not list chains individually. It references the deployments pages.
- Legacy file: https://github.com/Uniswap/v3-core/blob/main/bug-bounty.md (2021, up to $500k). It has been superseded by the Cantina program.

### 6.2 Admin / ownership (on-chain 2026-10-02 plus source)
- **v4 PoolManager is immutable.** It is not a proxy: the EIP-1967 slot is 0 on both chains, and `contract PoolManager is IPoolManager, ProtocolFees, ...` in https://github.com/Uniswap/v4-core/blob/main/src/PoolManager.sol. The owner's only power is `setProtocolFeeController` (`onlyOwner`, ProtocolFees.sol). Only the controller can `setProtocolFee` and `collectProtocolFees`. The protocol fee is capped at `MAX_PROTOCOL_FEE = 1000` pips (0.1%) per direction (src/libraries/ProtocolFeeLibrary.sol). LP funds and pool logic cannot be changed by admin.
- **v3 Factory:** the owner can only `setOwner` and `enableFeeAmount`. The factory owner can call `setFeeProtocol` on pools (each side 0 or 1/4–1/10 of the swap fee) and `collectProtocol`. Pools are not upgradeable. Source: https://github.com/Uniswap/v3-core/blob/main/contracts/UniswapV3Factory.sol and UniswapV3Pool.sol.

| Chain | Contract | Owner / controller (on-chain) | What it is |
|---|---|---|---|
| Base | v4 PoolManager `0x4985…2b2b` | `owner()` = `0x31FaFd4889FA1269F7a13A66eE0fB458f27D72A9` | **CrossChainAccount** (Sourcify 8453 exact_match). Storage: messenger = `0x4200…0007` (OP L2CrossDomainMessenger), `l1Owner` = `0x1a9C8182C09F50C8318d769245beA52c32BE35BC` = **Uniswap Governance Timelock (Ethereum)**. |
| Base | v4 `protocolFeeController()` | `0xc53dd825dedbe7814562528a1a30e8e5bcbb4b55` | **V4FeeAdapter** (Blockscout fully verified). Owner and feeSetter = the CrossChainAccount above. TOKEN_JAR `0x9bd2…168c`. |
| Base | v3 Factory `0x3312…FdfD` | `owner()` = `0xabea76658b205696d49b5f91b2a03536cb8a3be1` | **V3OpenFeeAdapter** (Sourcify 8453 exact_match). Owner and feeSetter = CrossChainAccount → L1 Timelock. Owner can `setFactoryOwner`, `enableFeeAmount`, `setFeeSetter`; anyone can trigger fee updates. |
| Robinhood | v4 PoolManager `0x8366…0951` | `owner()` = `0x2BAD8182C09F50C8318D769245BEA52C32BE46CD` (no code) | This is exactly **the Arbitrum-style L1→L2 alias** of `0x1a9C…35BC`: `0x1a9C…35BC + 0x1111000000000000000000000000000000001111`. That means the Uniswap Governance Timelock on Ethereum. |
| Robinhood | v4 `protocolFeeController()` | `0x6d0009504d129cf5002dba61d9ae8575aa79314c` | **V4FeeAdapter** (Sourcify 4663 exact_match). Owner and feeSetter = aliased L1 Timelock. TOKEN_JAR `0x2ac0…82f8`. |
| Robinhood | v3 Factory `0x1f7d…2EfA` | `owner()` = `0x05c420bc4823e039aa4da645edde743486daaa25` | Fee-adapter contract. `owner()` and `feeSetter()` = aliased L1 Timelock, `FACTORY()` = this factory. Its bytecode is 97.9% identical to the Base V3OpenFeeAdapter (same 7,266-byte size). **It is not on Sourcify for 4663, so the exact contract name is unconfirmed.** |

- Uniswap Governance Timelock `0x1a9C8182C09F50C8318d769245beA52c32BE35BC` (Ethereum) is listed in the Uniswap docs repo: https://github.com/Uniswap/docs/blob/main/content/ecosystem/governance/technical-reference.mdx ("Timelock", commit 1c7597d7). It is also named "Uniswap Governance Timelock" in `content/protocols/protocol-fee/deployments.mdx`.
  - `delay()` = **172,800 s (2 days)**. Read via `eth_call` selector `0x6a42b8f8` on the public Ethereum RPC `https://ethereum-rpc.publicnode.com`.
  - Proposals come from GovernorBravo `0x408ED6354d4973f66138C91495F2f2FCbd8724C3` (same docs page).
- In summary, every Uniswap admin power on both chains (protocol-fee switch and fee-tier enabling only) belongs to Uniswap on-chain governance through a 2-day L1 timelock and a cross-chain message. There is no multisig and no upgrade path for pools or PoolManager. Not checked: the Robinhood Chain bridge/inbox contract that delivers aliased L1 messages.
- Note: I could not fetch developers.uniswap.org/llms.txt (HTTP 429 rate limit). I used the Uniswap/docs GitHub repo instead.

### 6.3 DefiLlama
Both slugs from https://api.llama.fi/protocols (2026-10-02) have name "Uniswap V3" / "Uniswap V4", url `https://app.uniswap.org/` (the official Uniswap app domain), twitter `Uniswap` and parent `parent#uniswap`:
- **`uniswap-v3`**: chains include Base and Robinhood Chain.
- **`uniswap-v4`**: chains include Base and Robinhood Chain.

`uniswap-v2` also lists both chains.
Hacks: no entry for v3 or v4. Entries that exist: "Uniswap V1" 2020-04-18 reentrancy $220k, and "Uniswap (Phishing Attack)" 2022-07-11 $8M (social engineering, not a contract bug).

---

## 7. DefiLlama endpoints
- `https://api.llama.fi/protocols`: HTTP 200, no key, 8,464 protocols.
- `https://api.llama.fi/hacks`: **HTTP 200, no key needed**, JSON array of 1,293 records. Fields: date (unix), name, classification, technique, amount (USD), chain[], bridgeHack, targetType, source, returnedFunds, defillamaId, parentProtocolId, language. Match by `defillamaId` = the protocol's `id` from /protocols, or by `parentProtocolId`. Some entries have `defillamaId: null` and can only be matched by name.
- Merkl's own `protocol.trustData` (slug, hacks, audits) is copied from DefiLlama and **can be wrong**. For example, the Hyperdrive slug points to DELV. Match by url and twitter instead.

---

## 8. Summary table

| venue | chain | check method | contract | function | source URL | verified by eth_call | DefiLlama slug | hacks (DefiLlama) | bug bounty |
|---|---|---|---|---|---|---|---|---|---|
| Morpho Vault V2 | Base | factory registry | VaultV2Factory `0x4501125508079A99ebBebCE205DeC9593C2b5857` | `isVaultV2(address)` | https://docs.morpho.org/developers/contracts/addresses/ | yes (11 V2 vaults, plus Hyperdrive vault = 1; USDC control = 0) | morpho-blue | none on Morpho core (LeadBlock curator market 2024-10-14, $250k, oracle config) | Cantina $2.5M https://cantina.xyz/bounties/35a5f0a1-2ffd-432c-8f3b-77d169add8c3 |
| Morpho Vault V1.1 | Base | factory registry | MetaMorphoV1_1Factory `0xFf62A7c278C62eD665133147129245053Bbf5918` | `isMetaMorpho(address)` | same | yes (Jarvis = 1) | morpho-blue | same | same |
| Morpho Vault V1 (old) | Base | factory registry | MetaMorphoFactory `0xA9c3D3a366466Fa809d1Ae982Fb2c46E5fC41101` | `isMetaMorpho(address)` | same | yes (4 Moonwell V1 = 1) | morpho-blue | same | same |
| Morpho Vault V2 | Robinhood | factory registry | VaultV2Factory `0x0FBad98595b0186dA120E41f77C102beb49f803c` | `isVaultV2(address)` | same | yes (Steakhouse USDG V2 `0xBeEf…09dd` = 1) | morpho-blue (lists Robinhood Chain) | same | same |
| Morpho markets | Base / Robinhood | market exists | Morpho `0xBBBB…FFCb` / `0x9D53…1010` | `idToMarketParams(bytes32)` | same | yes (1 market each) | morpho-blue | same | same |
| Hyperdrive (Ambit Labs) | Base | Morpho V2 factory + official vault list | Vault `0x58a0f600d555eb25b792Ec2c6824D0bff5127B1F` (via VaultV2Factory `0x4501…5857`) | `isVaultV2(address)`; `owner()` / `curator()` match docs | https://docs.hyperdrive.finance/developers/contracts/ | yes (=1; owner, curator, sentinel and allocator match) | hyperdrive-hl-lending / hyperdrive-hl-earn (Base not tracked). **Not** `hyperdrive` (DELV) | Hyperdrive HL Lending 2025-09-27, $782k, access control (HyperEVM) | none found |
| IPOR Fusion | Base | factory registry for new vaults; otherwise official list or clone check | FusionFactory proxy `0x1455717668fA96534f675856347A973fA907e922`; CoreBase `0x7924…530a` | `isFusionVault(address)` (false for pre-IL-8227 vaults) | https://github.com/IPOR-Labs/ipor-abi/blob/main/mainnet/addresses.json and https://docs.ipor.io/build-on-fusion/developer-guide/addresses | yes: 19 Merkl vaults → isFusionVault = 0; 4 newest = 1; all 19 in ipor-abi; 12 are clones of CoreBase | fusion-by-ipor | Fusion by IPOR 2026-01-06, $336k, arbitrary external call (Arbitrum) | Immunefi https://immunefi.com/bug-bounty/ipor/information/ shows $1,000 max (text says $100K cap); Fusion is out of scope |
| IPOR Fusion | Robinhood | (no vaults yet) | FusionFactory proxy `0xDF7A590b60072476E355A8EfF33e4872440bDc7C` | `isFusionVault(address)` | ipor-abi mainnet-robinhood-fusion/addresses.json | no (not called) | fusion-by-ipor | same | same |
| Yieldseeker | Base | per-user wallet reverse lookup | AgentWalletFactory `0x9c7410a0faCAC60850C46eE5b58B518daec95130` | `owner()` + `ownerAgentIndex()` on wallet → `userWallets(address,uint256)` | https://github.com/tokenpage/yieldseeker-contracts/blob/main/deployments.json | yes (sample wallet matches; Merkl identifier 0xDCff… is an EOA) | yieldseeker | none | none found |
| Zia (v4 hook) | Robinhood | PoolId → `poolKeys` → hooks; hook source on Sourcify; PoolIds in official app bundle | ZiaFeeHook `0x64e9ae1066c47ac4a3cc0a5bd7b135908590e088` (address not on an official addresses page, so unverified) | `PositionManager.poolKeys(bytes25)`; hook `poolManager()`, `keeper()`, `treasury()` | https://repo.sourcify.dev/4663/0x64e9ae1066c47ac4a3cc0a5bd7b135908590e088 ; https://app.zia.finance/assets/v4PoolRouting-Ch7bROX2.js ; https://docs.zia.finance/liquidity/fees-and-apr.html | yes (8/8 PoolIds → this hook) | zia (0G only; Robinhood not tracked) | none | none found |
| Uniswap v4 | Base / Robinhood | core address match (already in chains/*.yaml) | PoolManager `0x4985…2b2b` / `0x8366…0951` | `owner()`, `protocolFeeController()` | https://developers.uniswap.org/deployments.json ; https://github.com/Uniswap/docs (technical-reference.mdx) | yes (owner = Uniswap L1 Timelock via CrossChainAccount / alias; 2-day delay) | uniswap-v4 | none (v4) | Cantina $15.5M https://cantina.xyz/bounties/f9df94db-c7b1-434b-bb06-d1360abdd1be |
| Uniswap v3 | Base / Robinhood | core address match | UniswapV3Factory `0x3312…FdfD` / `0x1f7d…2EfA` | `owner()` | same | yes (owner = fee adapter owned by Uniswap governance) | uniswap-v3 | none (v3) | Cantina, v3-core critical $2.25M (program max $15.5M) |

## 9. Things that are unconfirmed or need the owner's judgement
1. **Hyperdrive Base vault has zero timelocks.** Governance timelock min delay is 0, every vault timelock is 0, and there is no adapter registry, so the curator multisig (3 of 5) can change adapters instantly. A deployer EOA is also a proposer on the 0-delay timelock. This deserves a warning if the venue is added.
2. **Merkl's DefiLlama mapping for Hyperdrive is wrong.** It points to DELV's dead `hyperdrive`. The correct project's DefiLlama entries do not track Base at all.
3. **IPOR `isFusionVault` cannot recognize existing vaults.** Fall back to the ipor-abi list or the clone-target check. The docs' factory-implementation address is stale; ipor-abi and on-chain agree on `0x378f…4014`.
4. **Zia hook address** is attributed by verified source, the official app bundle and docs behavior, but it is **not** on an official addresses page. Its keeper and treasury are EOAs. Mark `unverified: true` for the address.
5. **Robinhood Steakhouse vault owner** `0xca50…db73` is a contract of unknown type: it has no `getThreshold()` or `getMinDelay()`, and Robinhood Blockscout is blocked by Cloudflare. **Robinhood v3 factory owner** `0x05c4…aa25` is functionally a V3 fee adapter, but it is not on Sourcify.
6. Morpho docs link to `robin.etherscan.io` for Robinhood, while our config uses Blockscout. Not investigated.
