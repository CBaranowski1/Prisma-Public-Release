# Rust — language grounding

This is **Rust**, a memory-safe systems language. Review it as Rust — do NOT
apply SQL, web, or shell concerns unless that code is actually present.

- **There is no SQL injection, XSS, or shell injection** in plain Rust logic.
  Only raise injection findings if the code actually builds SQL/HTML/shell
  strings from untrusted input (e.g. via `format!` into a DB query). Integer or
  slice arithmetic over local data has no injection surface.
- **Memory & ownership**: the borrow checker prevents use-after-free and data
  races at compile time — don't invent those. Do flag misuse of `unsafe`,
  needless `clone()`, or fighting the borrow checker.
- **Panics**: flag `unwrap()` / `expect()` / indexing that can panic on bad
  input; prefer `?`, `match`, `if let`, `get()`.
- **Integer overflow**: arithmetic can overflow (panics in debug, wraps in
  release); suggest `checked_*` / `saturating_*` where inputs are unbounded.
- **Idiom & clippy**: prefer iterator chains over manual loops, `&str` over
  `String` for params, avoid redundant `.collect()`. If it already passes
  `cargo clippy` cleanly, say so and do not invent style nits.
- **Error handling**: `Result`/`Option` propagation with `?`; avoid swallowing
  errors.

> If the code is already correct and idiomatic, return NO BUG FOUND — do not
> manufacture findings.
