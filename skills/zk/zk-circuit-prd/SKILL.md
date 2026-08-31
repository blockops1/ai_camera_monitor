---
name: zk-circuit-prd
description: "Write a PRD for a ZK circuit-based game. Covers proof system constraints, circuit architecture, and story structure when the circuit is a core game mechanic verified on-chain via zkVerify or similar."
version: 1.0.0
author: Jill Agent
license: MIT
---

# ZK Circuit PRD Writing

Write a Product Requirements Document for a game that uses a ZK circuit as a core mechanic, where the circuit is proven client-side and verified on-chain (e.g. via zkVerify).

## When to Use

- Writing a PRD that includes ZK circuit development
- Planning a game with on-chain ZK proof verification
- Switching ZK proof systems (e.g. Plonky2 → UltraHonk, Groth16 → Plonky2)
- Any story that involves nargo, barretenberg, wasm-pack, circom, or proof submission to a verification pallet

**Critical rule:** Before writing the circuit story, research the proof system's constraints. A bad choice of proof system cannot be fixed by writing better circuit code.

## Step 1: Choose the Proof System

Match the system to the verification platform. Common platforms and their supported proof systems:

### zkVerify Pallets (Horizen EVM)

| Pallet | Proof System | Key Constraints |
|--------|-------------|----------------|
| `settlementPlonky2Pallet` | Plonky2 | Poseidon transcript; recursive proofs OK |
| `settlementUltraplonkPallet` | Ultraplonk | Keccak256 transcript |
| `settlementUltrahonkPallet` | UltraHonk (barretenberg) | **Keccak256 only; NO recursion; zk flavor only** |
| `settlementGroth16Pallet` | Groth16 | Keccak256 or BN254; trusted setup required |
| `settlementSp1Pallet` | SP1 (Succinct) | RISC-V proof; larger proofs |

### Statement Hash (all zkVerify pallets)

```
context = keccak256(b"<proof_system_name>")
vk_hash = keccak256(vk.encode())
pubs_hash = keccak256(pubs)
statement_hash = context || vk_hash || pubs_hash  # concatenated
```

### Key Constraints Per System

**UltraHonk (Noir + barretenberg):**
- Only zk flavor: `bb generate --zk-prover`
- Only Keccak256 for transcript
- No recursion (flat circuit only)
- VK: barretenberg output from `bb write_vk -h <circuit_name>`
- Toolchain: `nargo` (compile) + `bb` (prove)
- Reference: https://docs.zkverify.io/architecture/verification_pallets/ultrahonk

**Plonky2 (Rust + wasm-pack):**
- Poseidon hash for all circuit hashing (NOT Keccak256)
- Recursion supported
- Client-side proving via `wasm-pack` (Rust → WASM)
- VK format: `{"config":"Poseidon","bytes":"<hex>"}`

**Groth16 (circom + snarkjs):**
- Requires trusted setup ceremony per circuit
- Keccak256 or BN254 pairing
- Smaller proofs but requires ceremony

## Step 2: Write the Circuit Story

Structure the circuit story with these sections:

### 2a. Public Inputs (on-chain verifiable)
Every value that will be published when the proof is verified:

```
- circuit_id: hash identifying this game configuration
- initial_money: uint32, in 10-cent units (1200 = $120.00)
- final_money: uint32, after all turns
- commitment: keccak256(weather_seed || price_seed)
- session_id: player's session identifier
- turn_count: always N (hardcoded check)
- ad_budget_total: total ad spend across all turns
- revenue_total: total revenue
- cost_total: total ingredient costs
```

### 2b. Private Witness (kept secret)
What stays private — never published on-chain:

```
- weather_seed: 256-bit random seed from drand beacon
- price_seed: 256-bit random seed
- Per turn (N turns): lemons_bought, sugar_bought, ice_bought, price_per_cup, ad_spend, customers, revenue
```

### 2c. Circuit Logic Functions

```
- derive_weather(seed, turn) -> weather category
  Uses keccak256(seed || turn) -> 0=rain, 1=cloudy, 2=sunny, 3=hot

- compute_demand(weather, price, ad_spend) -> customer count
  Weather multiplier * price multiplier * ad boost

- compute_revenue(customers, price, inventory) -> revenue
  min(customers, inventory) * price

- update_inventory(inventory, sales, weather) -> new inventory
  Ice melts: 50% on hot days, 20% otherwise

- verify_commitment(commitment, weather_seed, price_seed)
  Assert: commitment == keccak256(weather_seed || price_seed)
```

### 2d. Main Constraint
The core invariant the circuit enforces:

```
final_money == initial_money + revenue_total - cost_total - ad_budget_total
turn_count == N
ice melts to 0 by game end (no inventory hoarding)
```

### 2e. Hard Constraints
Non-negotiable game rules enforced as circuit constraints:
- Ice must melt to 0 (prevents hoarding)
- Price must be in valid range
- etc.

### 2f. Proof System-Specific Notes
Add system-specific constraints:
- UltraHonk: "NO RECURSION — design as single flat proof"
- Plonky2: "Poseidon for all circuit hashing — not Keccak256"

## Step 3: Write the Prover Story

Separately from the circuit. The prover:
- Takes game state (public + private)
- Runs the proof generation toolchain
- Returns `{ proof, vk, pubs, circuit_id }`
- Computes statement hash
- Submits to verification pallet

Include: toolchain commands, statement hash computation, progress UI, error handling.

## Step 4: Write the Contract Story

Smart contract that:
1. Accepts entry fee + commitment at game start
2. Calls verification pallet at game end
3. Manages leaderboard and reward distribution

## Step 5: Write Remaining Stories

Standard order after circuit/prover/contract core:
- Oracle integration (drand for randomness)
- Wiring game client -> prover -> contract
- UI implementation
- Deployment and testing
- Security analysis

## Common Mistakes

### 1. Choosing a system then discovering constraints mid-implementation
Research the pallet constraints BEFORE writing the circuit story.

### 2. Confusing public inputs vs private witness
- Public inputs: published on-chain when proof is verified
- Private witness: kept secret, only used in proof generation
- Weather seeds must be PRIVATE (players can't see weather before committing)

### 3. Wrong hash function for the transcript
- UltraHonk: Keccak256 only
- Plonky2: Poseidon only
Using the wrong hash = proof won't verify.

### 4. Designing a recursive circuit on a non-recursive system
UltraHonk explicitly forbids recursion. Design as flat.

### 5. Forgetting the statement hash computation
`statement_hash = context || vk_hash || pubs_hash` is computed off-chain in the prover.

## Story Template

```
## [US-00X] Write [SYSTEM] circuit for [GAME]

Current state: [what exists]

What to build:
Write the [SYSTEM] circuit that verifies [game mechanics].

Requirements:
1. PUBLIC INPUTS: [list]
2. PRIVATE WITNESS: [list]
3. CIRCUIT LOGIC: [functions]
4. HARD CONSTRAINTS: [non-negotiable rules]
5. PROOF SYSTEM NOTES: [system-specific constraints]

ZK [SYSTEM] specific notes:
- [key constraints]
- [toolchain]
- [VK format or generation]

Story type: create
Target file: [primary circuit file]
Depends on: [US-00X or none]
```

---

## ⚠️ Noir + UltraHonk Tooling Setup

> This section absorbed from `noir-ultrahonk-tooling` skill.

Install the Noir toolchain for UltraHonk ZK circuit development and proving. The two tools are `nargo` (Noir compiler) and `bb` (Barretenberg prover).

### The Two Tools

| Tool | Role | Package | Where it runs |
|------|------|---------|---------------|
| `nargo` | Noir compiler — writes circuits, compiles to ACIR | `nargo-<arch>-unknown-linux-gnu.tar.gz` | Docker container |
| `@aztec/bb.js` | Barretenberg WASM — browser-side proving | npm package | Browser (player device) |

**Note on `bb` CLI:** Both `nargo` AND `bb` must be installed in the Docker container. `nargo compile --backend barretenberg` invokes `bb` internally — without it, compilation fails.

### Installation in Dockerfile

**For zkVerify UltraHonk compatibility — use exact versions:**
- nargo: `v1.0.0-beta.6` (not nightly, not newer beta)
- bb: `v0.85.0` (>= 0.84.0, < 0.86.0)

```dockerfile
# nargo (Noir compiler) — must be v1.0.0-beta.6 for zkVerify
RUN curl -L https://github.com/noir-lang/noir/releases/download/v1.0.0-beta.6/nargo-aarch64-unknown-linux-gnu.tar.gz | tar -xz -C /usr/local/bin nargo && \
    chmod +x /usr/local/bin/nargo && \
    nargo --version

# bb (Barretenberg prover) — must be v0.85.x for zkVerify
RUN curl -L https://github.com/AztecProtocol/aztec-packages/releases/download/v0.85.0/barretenberg-arm64-linux.tar.gz | tar -xz -C /usr/local/bin && \
    chmod +x /usr/local/bin/bb && \
    bb --version
```

### keccak256 Dependency

Noir stdlib does NOT include Keccak256. For zkVerify (which requires Keccak256), add to `Nargo.toml`:
```toml
[dependencies]
keccak256 = { tag = "v0.1.3", git = "https://github.com/noir-lang/keccak256" }
```

### Cross-Platform Binary Matching

| Docker Host | Architecture | nargo Binary | bb Binary |
|-------------|-------------|--------------|-----------|
| Apple Silicon Mac | ARM64 | `nargo-aarch64-unknown-linux-gnu.tar.gz` | `barretenberg-arm64-linux.tar.gz` |
| x86_64 Linux | x86_64 | `nargo-x86_64-unknown-linux-gnu.tar.gz` | `barretenberg-amd64-linux.tar.gz` |

**Do NOT set `platform: linux/amd64` on ARM64 Mac** — forces slow QEMU emulation. Use native ARM64 binaries instead.

### Statement Hash Computation (for zkVerify)

```python
import sha3

def compute_statement_hash(vk_hex: str, pubs_hex: str, proof_system: str = "ultrahonk") -> str:
    context = sha3.keccak_256(b"ultrahonk").hexdigest()
    vk_hash = sha3.keccak_256(bytes.fromhex(vk_hex.lstrip("0x"))).hexdigest()
    pubs_hash = sha3.keccak_256(bytes.fromhex(pubs_hex.lstrip("0x"))).hexdigest()
    return "0x" + context + vk_hash + pubs_hash
```

### Full Proving Pipeline (working pattern)

```bash
# Step 1: nargo compile
docker compose -f ~/ralph/docker-compose.yml run --rm \
  -v ~/ralph/projects/lemonade-stand/circuits:/app/projects/lemonade-stand/circuits \
  -w /app/projects/lemonade-stand/circuits \
  --entrypoint bash ralph-local:latest -c "nargo compile && echo 'COMPILE OK'"

# Step 2: nargo execute (generates witness)
docker compose -f ~/ralph/docker-compose.yml run --rm \
  -v ~/ralph/projects/lemonade-stand/circuits:/app/projects/lemonade-stand/circuits \
  -w /app/projects/lemonade-stand/circuits \
  --entrypoint bash ralph-local:latest -c "nargo execute witness && echo 'EXECUTE OK'"

# Step 3: bb write_vk
docker compose -f ~/ralph/docker-compose.yml run --rm \
  -v ~/ralph/projects/lemonade-stand/circuits:/app/projects/lemonade-stand/circuits \
  -w /app/projects/lemonade-stand/circuits \
  --entrypoint bash ralph-local:latest -c "mkdir -p target/vk target/proof && bb write_vk -b target/lemonade_stand.json -o target/vk --scheme ultra_honk && echo 'VK OK'"

# Step 4: bb prove
docker compose -f ~/ralph/docker-compose.yml run --rm \
  -v ~/ralph/projects/lemonade-stand/circuits:/app/projects/lemonade-stand/circuits \
  -w /app/projects/lemonade-stand/circuits \
  --entrypoint bash ralph-local:latest -c "bb prove -b target/lemonade_stand.json -w target/lemonade_stand.gz -o target/proof --scheme ultra_honk && echo 'PROOF OK'"

# Step 5: bb verify
docker compose -f ~/ralph/docker-compose.yml run --rm \
  -v ~/ralph/projects/lemonade-stand/circuits:/app/projects/lemonade-stand/circuits \
  -w /app/projects/lemonade-stand/circuits \
  --entrypoint bash ralph-local:latest -c "bb verify -p target/proof/proof -k target/vk/vk -i target/proof/public_inputs --scheme ultra_honk && echo 'VERIFY OK'"
```

### Known keccak256 Bug

**keccak256 v0.1.3 has a padding bug at exactly 136 bytes** — the second block gets zeroed. If keccak256 output is wrong, check if your input is 136 bytes. Workaround: use input sizes < 136 bytes or > 136 bytes.

### Key Constraints for UltraHonk (from zkVerify docs)
- **Hash:** Keccak256 only (via `std::hash::keccak256()` in Noir)
- **Recursion:** NOT supported — design as flat circuit
- **Proof flavor:** Must use `--zk-prover` / `--zk-verifier` flags
- **Statement:** `context || vk_hash || pubs_hash` (concatenated, not nested hash)
- **VK format:** raw hex output from `bb write_vk`

---

## ⚠️ Plonky2 Circuit Development

> This section absorbed from `plonky2-circuit-development` skill.

### When to Use
- Building ZK circuits with Plonky2
- Writing constraint functions for Plonky2
- Working with `CircuitBuilder`, `Target`, `HashOutTarget`, `PartialWitness`

### Key API Facts (Plonky2 1.1.0)

**Trait Hierarchy:**
```
Field (zero, add, sub, mul, div)
  └── RichField (Plonky2 field with Poseidon): impl HashOut, PrimeField64, Clone, Copy
        └── PrimeField64: to_canonical_u64(), from_canonical_u64(n)
```

**CircuitBuilder Methods (verified available):**
```rust
// Arithmetic
builder.add(a, b)           // Target + Target → Target
builder.sub(a, b)           // Target - Target
builder.mul(a, b)           // Target * Target
builder.div(a, b)           // Target / Target (floor)

// Constants and public inputs
builder.add_virtual_public_input()           // returns Target
builder.connect(a, b)                        // a == b constraint
builder.connect_array(a, b)                  // arrays equal element-wise

// Range and decomposition
builder.range_check(x, n_bits)
builder.split_le(x, n_bits)       // returns Vec<BoolTarget>

// Hashing (Poseidon) — WRONG method name on builder
builder.hash_n_to_hash_no_pad::<PoseidonHash>(targets)  // Vec<Target>, NOT slice
// NOT: builder.hash_no_pad() — that doesn't exist

// Comparison
builder.is_equal(a, b)           // returns Target (1 if equal)
builder._if(cond, then_val, else_val)  // Target-level conditional
```

**CPU-side Hashing:**
```rust
use plonky2::hash::poseidon::PoseidonHash;
use plonky2::hash::AlgebraicHasher;

PoseidonHash::hash_no_pad(inputs: &[F]) -> HashOut<F>  // requires F: RichField
```

### Common Mistakes

**1. Wrong generic bound — Field vs RichField**
```rust
// WRONG — Field doesn't implement PoseidonHash::hash_no_pad
pub fn my_hash<F: Field>(...) -> HashOut<F> { ... }

// CORRECT
pub fn my_hash<F: RichField>(...) -> HashOut<F> { ... }
```

**2. Non-existent builder methods**
```rust
// WRONG — add_sub does not exist
let result = builder.add_sub(x, z, y);

// CORRECT
let tmp = builder.sub(x, y);
let result = builder.add(tmp, z);
```

**3. Wrong hash method on builder**
```rust
// WRONG
builder.hash_no_pad::<PoseidonHash>(&targets)

// CORRECT — Vec, not slice
builder.hash_n_to_hash_no_pad::<PoseidonHash>(targets)
```

**4. Wrong import path**
```rust
// WRONG
use plonky2::plonk::circuit::CircuitBuilder;

// CORRECT
use plonky2::plonk::circuit_builder::CircuitBuilder;
```

**5. Target is Copy — no clone needed**
```rust
// WRONG
let x = t.target.clone();

// CORRECT
let x = t.target;
```

**6. BoolTarget → Target conversion**
```rust
let bits = builder.split_le(x, 2);
let bit0: Target = bits[0].target;   // BoolTarget → Target via .target
```

### API Discovery Process

When docs are wrong or missing, inspect the cargo registry source:
```bash
find ~/.cargo/registry/src -name "plonky2-1.1.0" -type d
# Key files to inspect:
# plonky2-1.1.0/src/plonk/circuit_builder.rs
# plonky2-1.1.0/src/hash/poseidon.rs
# plonky2-1.1.0/src/iop/witness.rs
```
