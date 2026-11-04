# Contributing to PolyPulse

Thank you for your interest in contributing to PolyPulse! We welcome contributions from systems engineers, quantitative developers, and frontend designers.

Please read this document to understand our contribution workflow, coding standards, branch conventions, and testing requirements before submitting a pull request.

---

## 1. Code of Conduct

All contributors and maintainers are expected to adhere to our [Code of Conduct](CODE_OF_CONDUCT.md). Please maintain a respectful, welcoming, and inclusive environment.

---

## 2. Getting Started & Development Setup

### Prerequisites
- **Python 3.11+**
- **Node.js 20+** and **npm**
- **C++20 Compiler**: `g++` (GCC 11+) or `clang++` (Clang 13+)
- **Docker Desktop** (optional, for ClickHouse testing)

### Local Setup
```powershell
# 1. Fork and clone the repository
git clone https://github.com/your-username/polypulse.git
cd polypulse

# 2. Set up Python virtual environment
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt

# 3. Build the C++20 engine and run engine tests
python scripts/build_engine.py --test

# 4. Set up frontend dependencies
cd frontend
npm install
npm run build
cd ..

# 5. Run test suites
python -m unittest discover -s tests -v
```

---

## 3. Branch Naming Conventions

All feature and bugfix work should occur on a dedicated branch created from `master`:

| Prefix | Description | Example |
| :--- | :--- | :--- |
| `feat/` | New features or engine capabilities | `feat/avx2-vwap-kernel` |
| `fix/` | Bug fixes and crash remediations | `fix/kalshi-heartbeat-timeout` |
| `perf/` | Performance optimizations | `perf/vector-flat-array-alignment` |
| `docs/` | Documentation updates | `docs/clickhouse-asof-guide` |
| `refactor/` | Code refactoring without behavior change | `refactor/normalizer-dispatch` |
| `test/` | Adding or improving test suites | `test/orderbook-sequence-fuzzing` |

---

## 4. Commit Message Guidelines

We enforce the [Conventional Commits](https://www.conventionalcommits.org/) specification:

```text
<type>(<scope>): <short description in imperative present tense>

[optional body explaining motivation and trade-offs]

[optional footer(s), e.g., Closes #12]
```

### Supported Types:
- `feat`: A new feature or endpoint.
- `fix`: A bug fix.
- `perf`: A code change that improves latency, throughput, or memory usage.
- `docs`: Documentation-only changes.
- `style`: Changes that do not affect code logic (formatting, whitespace).
- `refactor`: Code changes that neither fix a bug nor add a feature.
- `test`: Adding missing tests or correcting existing tests.
- `chore`: Build scripts, dependencies, CI configuration.

### Examples:
- `feat(engine): add SIMD vectorized batch VWAP kernel`
- `fix(connectors): reset silence watchdog timer on PONG frame`
- `docs(readme): add architecture sequence diagram`

---

## 5. Coding Standards & Invariants

### C++ Engine (`engine/`)
- Follow modern **C++20** idioms.
- **Zero Allocations on Hot Paths**: Hot execution functions (`pp_set_level`, `pp_quote`, `pp_vwap`) must avoid dynamic heap allocations.
- Fixed-point arithmetic: Prices and quantities are integer ticks scaled by $10^4$. Never introduce raw floating-point prices into internal state structures.
- Strict sequence monotonicity: Invalidate or reject out-of-order updates.

### Python Services (`backend/`)
- Adhere to **PEP 8** style guidelines.
- Use explicit type annotations for function signatures.
- Never block the AsyncIO event loop with synchronous file I/O or network requests.
- All database commits to SQLite must preserve WAL mode.

### Frontend (`frontend/`)
- Write strictly typed TypeScript (no implicit `any`).
- Decouple high-frequency WebSocket streams into Web Workers to prevent React render lag.
- Respect the dark technical aesthetic and design tokens.

---

## 6. Submitting a Pull Request (PR)

1. Ensure all tests pass before opening a PR:
   ```powershell
   python -m unittest discover -s tests -v
   python scripts/build_engine.py --test
   cd frontend; npm run build; cd ..
   ```
2. Verify benchmark stability:
   ```powershell
   python scripts/benchmark.py
   ```
3. Push your branch to your fork:
   ```powershell
   git push origin feat/your-feature
   ```
4. Open a PR against `shatwiks/polypulse:master` using the provided [Pull Request Template](.github/PULL_REQUEST_TEMPLATE.md).
5. Ensure CI checks pass on GitHub Actions.
