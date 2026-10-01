# Pascal Typing: Keystroke Trace Simulator & Auto-Typer

A high-fidelity keystroke trace simulator and auto-typer featuring motor kinematics (Fitts' Law ID, hand alternation), cognitive models (Ornstein-Uhlenbeck pace drift, fatigue, log-normal dwells, error episodes), editor auto-indent integration, and an intelligent **Coding Mode** with Pascal `begin`/`end` lookahead.

## Key Features

1. **Stochastic Timing & Kinematic Modeling**
   - **Inter-Key Interval (IKI)**: Gammavariate distributions parameterized by WPM, Fitts' difficulty index, hand alternation, digraph speed-ups, syntax delays, and shift modifier reach penalties.
   - **Dwell Time**: Log-normally distributed key hold durations adapted to current pace and key type.
   - **Pace Drift & Fatigue**: Exact Ornstein-Uhlenbeck discretisation with mean-reversion and workload/recovery dynamics.
   - **Error Episodes**: Naturalistic typing errors (substitution, transposition, omission, insertion, overshoot) with realization pauses and backspace corrections.
   - **Natural Thinking Pauses**: Pauses occur naturally at word boundaries rather than within runs of whitespace.

2. **Editor Auto-Indent Policies**
   - `"off"`: Types text verbatim without deleting indentation.
   - `"copy"`: Automatically predicts and clears indentation copied from the preceding line.
   - `"smart"`: Predicts additional indentation levels opened by block characters (`:`, `{`, `(`, `[`) or Pascal keywords (`begin`, `then`, `do`, `try`, etc.).
   - `"fixed"`: Issues a fixed count of backspaces after each newline.
   - `tab_stop_backspace`: Supports editor behavior where Backspace removes full indentation levels at once (e.g. VS Code smart backspace).

3. **Coding Mode (Pascal / Structured Code Lookahead)**
   - Scans forward to detect matching `begin` ... `end` blocks (with full support for nested blocks, comments, and string literals).
   - Types out the block frame (`begin`, newline, `end`), navigates back up (`<UP>`), fills in all body statements in logical order, and navigates down (`<DOWN>`) past the closing `end`.
   - Produces 100% exact source code in the target editor while capturing authentic coding behavior.

## CLI Usage

### Run Benchmark (Simulated, no keys pressed)
```bash
python auto_typer.py --benchmark --wpm 110 --mode net --typo 3.0 --seed 12345
```

### Benchmark Coding Mode on a Pascal File
```bash
python auto_typer.py --benchmark --coding-mode --file program.pas --indent smart
```

### Export Keystroke Trace to CSV
```bash
python auto_typer.py --benchmark --wpm 100 --seed 42 --csv trace.csv --coding-mode --file program.pas
```

## Running the Test Suite

```bash
python -m unittest test_auto_typer.py
```
