import io
import os
import random
import re
import tempfile
import unittest
from auto_typer import (
    TypingProfile,
    TypingModel,
    TypingPlanner,
    TypingState,
    IndentPolicy,
    Event,
    Transition,
    events_to_transitions,
    find_pascal_blocks,
    expected_auto_indent,
    build_trace,
    simulate,
    run_benchmark,
    summarize_trace,
    export_trace_csv,
    TracePlayer,
    PlaybackPolicy,
    UP,
    DOWN,
    LEFT,
    RIGHT,
    HOME,
    END_KEY,
    BACKSPACE,
    ENTER,
    SHIFT_L,
    SHIFT_R,
    MIN_IKI,
    MIN_GAP,
    _SHIFT_MAP,
    hand,
    motor_hand,
    get_key_distance,
    fitts_index_of_difficulty,
)

_REV_SHIFT = {v: k for k, v in _SHIFT_MAP.items()}


def _shift_key(key: str) -> str:
    if key in _REV_SHIFT:
        return _REV_SHIFT[key]
    if isinstance(key, str) and key.isalpha():
        return key.upper()
    return key


class VirtualEditor:
    """Simulates an interactive code editor responding to physical keystroke transitions."""
    def __init__(self, indent_policy: IndentPolicy = None, unit: str = "    "):
        self.lines = [""]
        self.row = 0
        self.col = 0
        self.indent_policy = indent_policy or IndentPolicy()
        self.unit = unit
        self.shift_held = False

    def on_transition(self, tr: Transition):
        if tr.key in (SHIFT_L, SHIFT_R):
            self.shift_held = tr.down
            return

        if not tr.down:
            return

        key = tr.key
        if key == ENTER or key == "\n":
            left = self.lines[self.row][:self.col]
            right = self.lines[self.row][self.col:]
            self.lines[self.row] = left

            auto = expected_auto_indent(left, self.indent_policy.mode, self.unit)
            self.lines.insert(self.row + 1, auto + right)
            self.row += 1
            self.col = len(auto)
        elif key == BACKSPACE:
            if self.col > 0:
                cur = self.lines[self.row]
                self.lines[self.row] = cur[:self.col - 1] + cur[self.col:]
                self.col -= 1
            elif self.row > 0:
                prev_len = len(self.lines[self.row - 1])
                self.lines[self.row - 1] += self.lines[self.row]
                self.lines.pop(self.row)
                self.row -= 1
                self.col = prev_len
        elif key == UP:
            if self.row > 0:
                self.row -= 1
                self.col = min(self.col, len(self.lines[self.row]))
        elif key == DOWN:
            if self.row < len(self.lines) - 1:
                self.row += 1
                self.col = min(self.col, len(self.lines[self.row]))
        elif key == END_KEY:
            self.col = len(self.lines[self.row])
        elif key == HOME:
            self.col = 0
        elif key == LEFT:
            if self.col > 0:
                self.col -= 1
        elif key == RIGHT:
            if self.col < len(self.lines[self.row]):
                self.col += 1
        else:
            ch = _shift_key(key) if self.shift_held else key
            cur = self.lines[self.row]
            self.lines[self.row] = cur[:self.col] + ch + cur[self.col:]
            self.col += len(ch)

    def text(self) -> str:
        return "\n".join(self.lines)


def replay_events_in_editor(events, indent_policy=None, unit="    ") -> str:
    """Replay transitions through a virtual editor with shift and auto-indent tracking."""
    editor = VirtualEditor(indent_policy, unit)
    for tr in events_to_transitions(events):
        editor.on_transition(tr)
    return editor.text()


class MockKeyboard:
    def __init__(self):
        self.log = []
        self.currently_held = set()

    def resolve(self, key):
        return key

    def press(self, key):
        self.currently_held.add(key)
        self.log.append(("press", key))

    def release(self, key):
        if key in self.currently_held:
            self.currently_held.remove(key)
        self.log.append(("release", key))


class TestAutoTyper(unittest.TestCase):

    def test_reproducibility_with_seed(self):
        """Same seed must produce the exact same sequence of events."""
        text = "procedure Hello;\nbegin\n    Writeln('Hello World');\nend;"
        rng1 = random.Random(42)
        rng2 = random.Random(42)
        events1, _ = build_trace(text, 100, 0.05, IndentPolicy("smart"), rng1, coding_mode=True)
        events2, _ = build_trace(text, 100, 0.05, IndentPolicy("smart"), rng2, coding_mode=True)
        self.assertEqual(len(events1), len(events2))
        for e1, e2 in zip(events1, events2):
            self.assertEqual(e1.action, e2.action)
            self.assertEqual(e1.char, e2.char)
            self.assertAlmostEqual(e1.press_at, e2.press_at, places=7)
            self.assertAlmostEqual(e1.release_at, e2.release_at, places=7)

    def test_different_seeds_produce_different_traces(self):
        text = "The quick brown fox jumps over the lazy dog."
        rng1 = random.Random(101)
        rng2 = random.Random(202)
        events1, _ = build_trace(text, 100, 0.05, IndentPolicy("off"), rng1)
        events2, _ = build_trace(text, 100, 0.05, IndentPolicy("off"), rng2)
        t1 = [e.press_at for e in events1]
        t2 = [e.press_at for e in events2]
        self.assertNotEqual(t1, t2)

    def test_timing_invariants(self):
        """All dwell times > 0, all releases >= presses, all intervals valid."""
        text = "Hello, world! 123 + 456 = 579.\nSecond line with indentation.\n"
        rng = random.Random(123)
        events, _ = build_trace(text, 120, 0.08, IndentPolicy("smart"), rng)
        for ev in events:
            self.assertGreater(ev.release_at, ev.press_at)
            if ev.action == "key":
                self.assertGreaterEqual(ev.dwell, 0.015)
        transitions = events_to_transitions(events)
        for i in range(1, len(transitions)):
            self.assertGreaterEqual(transitions[i].at, transitions[i - 1].at)

    def test_virtual_editor_replay_accuracy_standard_mode(self):
        """Standard mode typing must reproduce the original text in the editor."""
        code = (
            "def factorial(n):\n"
            "    if n <= 1:\n"
            "        return 1\n"
            "    return n * factorial(n - 1)"
        )
        for mode in ("off", "copy", "smart"):
            policy = IndentPolicy(mode)
            rng = random.Random(77)
            events, _ = build_trace(code, 90, 0.0, policy, rng, coding_mode=False)
            result = replay_events_in_editor(events, policy)
            self.assertEqual(result, code)

    def test_virtual_editor_replay_accuracy_coding_mode_simple(self):
        """Coding mode with simple Pascal begin...end block."""
        pascal = (
            "procedure DoWork;\n"
            "begin\n"
            "    x := 10;\n"
            "    y := 20;\n"
            "end;"
        )
        for mode in ("smart", "copy", "off"):
            policy = IndentPolicy(mode)
            rng = random.Random(99)
            events, _ = build_trace(pascal, 100, 0.0, policy, rng, coding_mode=True)
            result = replay_events_in_editor(events, policy)
            self.assertEqual(result, pascal)

    def test_virtual_editor_replay_accuracy_nested_blocks(self):
        """Coding mode with nested Pascal blocks."""
        pascal = (
            "program NestedDemo;\n"
            "procedure Outer;\n"
            "begin\n"
            "    if x > 0 then\n"
            "    begin\n"
            "        x := x - 1;\n"
            "        Writeln('Decreased');\n"
            "    end;\n"
            "    Writeln('Outer done');\n"
            "end;\n"
            "begin\n"
            "    Outer;\n"
            "end."
        )
        for mode in ("smart", "copy", "off"):
            policy = IndentPolicy(mode)
            rng = random.Random(54321)
            events, _ = build_trace(pascal, 110, 0.0, policy, rng, coding_mode=True)
            result = replay_events_in_editor(events, policy)
            self.assertEqual(result, pascal)

    def test_coding_mode_with_typos(self):
        """Coding mode with typos and corrections still accurately finishes the text."""
        pascal = (
            "begin\n"
            "    a := 1;\n"
            "    b := 2;\n"
            "end;"
        )
        policy = IndentPolicy("smart")
        rng = random.Random(42)
        events, _ = build_trace(pascal, 90, 0.08, policy, rng, coding_mode=True)
        result = replay_events_in_editor(events, policy)
        self.assertEqual(result, pascal)

    def test_empty_block_and_single_line(self):
        """Empty begin...end and single-line code."""
        pascal = (
            "begin\n"
            "end;"
        )
        policy = IndentPolicy("off")
        rng = random.Random(111)
        events, _ = build_trace(pascal, 100, 0.0, policy, rng, coding_mode=True)
        result = replay_events_in_editor(events, policy)
        self.assertEqual(result, pascal)

    def test_strings_and_comments_ignored_by_block_finder(self):
        """'begin' and 'end' inside strings and comments do not create false blocks."""
        lines = [
            "procedure Test;",
            "begin",
            "    msg := 'begin and end in string';",
            "    // begin comment",
            "    (* end comment *)",
            "    Writeln(msg);",
            "end;",
        ]
        blocks = find_pascal_blocks(lines)
        self.assertEqual(blocks, {1: 6})

    def test_lookahead_limit(self):
        """Blocks farther than max_lookahead are ignored and typed sequentially."""
        lines = ["begin"] + [f"    x := {i};" for i in range(15)] + ["end;"]
        blocks_short = find_pascal_blocks(lines, max_lookahead=5)
        self.assertEqual(blocks_short, {})
        blocks_long = find_pascal_blocks(lines, max_lookahead=20)
        self.assertEqual(blocks_long, {0: 16})

    def test_benchmark_simulation_gross_and_net(self):
        """Benchmark runs cleanly in both net and gross modes."""
        stats_net = run_benchmark("begin\n    x := 1;\nend;", wpm=100, typo_rate=0.03,
                                  indent=IndentPolicy("smart"), seed=123, mode="net", coding_mode=True)
        self.assertEqual(stats_net["mode"], "net")
        self.assertTrue(stats_net["coding_mode"])
        self.assertGreater(stats_net["net_wpm"], 0)

        stats_gross = run_benchmark("begin\n    x := 1;\nend;", wpm=100, typo_rate=0.03,
                                    indent=IndentPolicy("smart"), seed=123, mode="gross", coding_mode=True)
        self.assertEqual(stats_gross["mode"], "gross")
        self.assertGreater(stats_gross["gross_wpm"], 0)

    def test_csv_export(self):
        """export_trace_csv writes valid CSV output."""
        events, _ = build_trace("Test line", 100, 0.0, IndentPolicy("off"), random.Random(1))
        with tempfile.NamedTemporaryFile("w+", delete=False, suffix=".csv") as tf:
            path = tf.name
        try:
            export_trace_csv(events, path)
            with open(path, "r", encoding="utf-8") as f:
                content = f.read()
            self.assertIn("action,char,press_at,release_at,line,note,key", content)
            self.assertIn("key,T,", content)
            self.assertIn("key,e,", content)
        finally:
            if os.path.exists(path):
                os.remove(path)

    def test_motor_geometry_and_nav_keys(self):
        """Navigation keys have valid geometry and motor hand assignments."""
        for k in (UP, DOWN, LEFT, RIGHT, HOME, END_KEY):
            self.assertEqual(motor_hand(k), "R")
            self.assertGreater(get_key_distance("a", k), 0)
            self.assertGreater(fitts_index_of_difficulty("a", k), 0)


if __name__ == "__main__":
    unittest.main()
