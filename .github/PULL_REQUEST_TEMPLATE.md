## Description

Provide a clear and concise summary of the changes proposed in this Pull Request. Explain the motivation and link any related issues.

Fixes #(issue)

---

## Type of Change

Please delete options that are not relevant:

- [ ] `bugfix`: Bug fix that repairs incorrect behavior without breaking existing functionality
- [ ] `feature`: New capability or endpoint
- [ ] `perf`: Latency or throughput optimization
- [ ] `refactor`: Internal refactoring with no public behavior change
- [ ] `docs`: Documentation improvement or correction
- [ ] `test`: New tests or benchmark additions

---

## Quantitative & Engineering Invariants Checklist

Please confirm that your changes adhere to PolyPulse engineering standards:

- [ ] **Numeric Determinism**: All prices and quantities respect the $10^4$ fixed-point integer tick scale (no raw floats in hot orderbook loops).
- [ ] **Cache Locality**: No dynamic heap allocations (`new`, `malloc`) introduced on hot analytical paths (`set_level`, `vwap`, `quote`).
- [ ] **Sequence Monotonicity**: Out-of-order sequence frames are properly rejected or trigger resubscription.
- [ ] **Null Safety**: Unobserved future midpoints in markout evaluations are represented as `null`, avoiding lookahead bias.
- [ ] **Local Durability**: SQLite commits preserve WAL mode (`PRAGMA journal_mode=WAL`).

---

## Verification & Testing Performed

Describe the tests executed to verify your changes:

```powershell
# 1. C++ Engine Tests
python scripts/build_engine.py --test

# 2. Python Pipeline & Normalization Tests
python -m unittest discover -s tests -v

# 3. Frontend Production Build
cd frontend; npm run build; cd ..

# 4. Microbenchmark Suite
python scripts/benchmark.py
```

- [ ] C++ unit tests pass
- [ ] Python unit & normalization tests pass
- [ ] Frontend builds without TypeScript errors
- [ ] Microbenchmarks execute with no latency regression
