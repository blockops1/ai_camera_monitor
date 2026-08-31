---
name: blockchain-app-dev
description: "Blockchain application development: Solidity smart contract security review (Slither + Foundry) and Python Web3 RPC resilience patterns. Covers security triage, Foundry test patterns, retry/failover for Ethereum RPC calls, and on-chain data extraction."
trigger: "blockchain, smart contract, Solidity, Web3.py, Ethereum RPC, Slither, Foundry, forge, on-chain data"
version: 1.0.0
---

# Blockchain Application Development

Covers two complementary concerns: **smart contract security** (Slither + Foundry) and **Python Web3 RPC resilience** (retry wrappers, failover).

---

## ⚠️ Solidity Security Review

> Absorbed from `solidity-security-review` skill.

### Toolchain (run in order)

```bash
# 1. Format check
forge fmt

# 2. Linting
solhint src/*.sol

# 3. Static analysis — Slither
slither . --solc-remaps "lib=node_modules/forge-std:lib"

# 4. Dynamic testing — Foundry
forge test -vvv
```

### Slither Finding Triage

#### Almost Always False Positives
| Detector | Reason |
|----------|--------|
| `weak-prng` | `ts - (ts % 86400)` is a day-boundary calculation, not a PRNG |
| `uninitialized-state` (mappings) | Solidity mappings auto-initialize to zero; no explicit init needed |
| `timestamp` | Using `block.timestamp` for game-day logic is intentional and acceptable |
| `divide-before-multiply` | Solidity 0.8+ integer division rounds predictably; gas only |
| `unused-import` | Minor style issue |
| `solc-version` | pragma is informational; compiler version actually used is what matters |
| `immutable-states` | Gas optimization, not a vulnerability |

#### Real Issues — Fix First
| Detector | Location | Risk | Action |
|----------|----------|------|--------|
| `reentrancy` | `.call()` without checks-effects | CRITICAL | Add reentrancy guard (OpenZeppelin `ReentrancyGuard`) |
| `uninitialized-state` | storage vars (non-mapping) | HIGH | Initialize in constructor |
| `arbitrary-send-eth` | `.transfer()`/.`call()` in loop | HIGH | Use pull-payment pattern |
| `calls-loop` | external calls in loop | MEDIUM | Extract to function, check effects |
| `incorrect-equality` | strict `==` in money calc | **HIGH** | `==` in loops returning max/min values is wrong — should use `>=` / `<=` to track accumulator correctly |
| `low-level-calls` | no return value check | MEDIUM | Always check `success` return |
| `array-copy` | dynamic array access before bounds check | HIGH | Never write to `arr[insertPos]` when `arr.length == 0` — use `arr.push()` first |

### Real Bug Patterns Found

**`incorrect-equality` — accumulator bug:**
```solidity
// WRONG: returns first match
if (playerSession.finalMoney == finalMoney) {
    return finalMoney;
}
// RIGHT: tracks highest
if (playerSession.finalMoney > highest) {
    highest = playerSession.finalMoney;
}
return highest;
```

**Dynamic array OOB in leaderboard insertion:**
```solidity
// WRONG: if leaderboard is empty (length=0), this writes to index 0 of a 0-length array → reverts
leaderboard[insertPos] = player;
// RIGHT: push first, then shift and overwrite
leaderboard.push(player);
for (uint i = leaderboard.length - 1; i > insertPos; i--) {
    leaderboard[i] = leaderboard[i - 1];
}
leaderboard[insertPos] = player;
```

### Foundry Test Patterns

**Mocking an unimplemented verifier (e.g. zkVerify pallet):**
```solidity
MockVerifier mockVerifier = new MockVerifier();
vm.etch(address(0xdeadbeef), address(mockVerifier).code);
```

**`submitProof` requires >=3 public inputs:**
```solidity
function _pubs(uint256 finalMoney) internal pure returns (bytes32[] memory) {
    bytes32[] memory pubs = new bytes32[](3);
    pubs[0] = bytes32(0);
    pubs[1] = bytes32(uint256(1));
    pubs[2] = bytes32(finalMoney);
    return pubs;
}
```

### Forge Test Hangs — Debug Steps

1. Run with `-vvv` and a timeout: `timeout 60 forge test -vvv`
2. Check for infinite loops in fuzz tests (`vm.fuzz`)
3. Check for tests that call external contracts that hang
4. Isolate: `forge test --match-test testName -vvv`

---

## ⚠️ Python 3.9 Compatibility

System Python is **3.9.6** (`/usr/bin/python3`). Always use `Optional[X]` syntax, **never** `X | None`.

```python
# WRONG — syntax error on 3.9
def foo(x: str | None) -> int | None:

# CORRECT
from typing import Optional
def foo(x: Optional[str]) -> Optional[int]:
```

Import `Optional` from `typing`. This applies to all files in any project on this machine — production, staging, ralph workspace, everything.

---

## ⚠️ Web3 RPC Resilience

> Absorbed from `web3-call-retry` skill.

### Retry Pattern for web3.py

web3.py bound methods can't be passed directly as callables to a retry wrapper — wrap in a lambda:

```python
from web3 import Web3
import time

def _retry_web3_call(w3, func, *args, retries=3, delay=1.0, context='', **kwargs):
    """
    Retry a Web3 RPC call with exponential backoff.
    func: a lambda wrapping the bound method, e.g. lambda: w3.eth.get_balance(addr)
    context: human-readable label for logging/alerting.
    """
    last_exc = None
    for attempt in range(retries):
        try:
            return func(*args, **kwargs)
        except Exception as e:
            last_exc = e
            if attempt < retries - 1:
                wait = delay * (2 ** attempt)
                print(f'[RETRY] {context} (attempt {attempt+1}/{retries}): {e}')
                time.sleep(wait)
    print(f'[ALERT] {context} FAILED after {retries} retries: {last_exc}')
    raise last_exc

# Usage:
balance_wei = _retry_web3_call(
    w3,
    lambda: w3.eth.get_balance(address),
    retries=3, delay=1.0,
    context=f'get_balance({address[:10]}...)'
)
```

### RPC Health + Failover Pattern

```python
_RPC_LIST = ['https://base-mainnet.g.alchemy.com/v2/KEY', 'https://base.publicrpc.com']
_rpc_primary_index = 0

def _get_web3():
    global _rpc_primary_index
    for offset in range(len(_RPC_LIST)):
        idx = (_rpc_primary_index + offset) % len(_RPC_LIST)
        url = _RPC_LIST[idx]
        try:
            start = time.monotonic()
            w3 = Web3(Web3.HTTPProvider(url, request_kwargs={'timeout': 10}))
            w3.eth.block_number  # validate
            latency_ms = (time.monotonic() - start) * 1000
            _rpc_primary_index = idx
            return w3
        except Exception:
            continue
    raise ConnectionError('All RPC endpoints unreachable')
```

### Key Rules
- **Never pass bound methods directly** — `w3.eth.get_transaction_count` fails as a callable in retry wrappers; always wrap in `lambda: w3.eth.get_transaction_count(...)`
- **Alert on exhaustion** — retry-exhausted case indicates systemic RPC failure
- **Circuit-breaker:** for balance checks return safe default (`0.0`); for nonce/gas/estimation raise; for tx submission the outer retry loop handles it
- **Exponential backoff** — `delay * (2 ** attempt)` for transient network errors

### When to Use
- Any Python script calling Ethereum RPC methods (`eth_getBalance`, `eth_call`, `eth_sendRawTransaction`, `eth_estimateGas`, etc.)
- Cron jobs, background daemons, trading bots where a single RPC blip shouldn't crash the process
- Also applies to `contract.functions.myMethod().call()` — those are still RPC calls
