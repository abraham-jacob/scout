"""Tests for agent/llm_common.py — cross-cutting LLM-layer plumbing."""

from agent.llm_common import _add_usage, print_token_summary


class TestTokenTracking:
    """Test token and cost tracking."""

    def test_add_usage_increments_tokens(self):
        """Add usage increments token counters."""
        from agent.llm_common import _tokens, _tokens_lock

        with _tokens_lock:
            initial_input = _tokens["input"]
            initial_output = _tokens["output"]

        usage = {
            "input_tokens": 100,
            "output_tokens": 50,
            "cache_read_input_tokens": 200,
            "cache_creation_input_tokens": 150,
        }

        _add_usage(usage, 0.05)

        with _tokens_lock:
            assert _tokens["input"] == initial_input + 100
            assert _tokens["output"] == initial_output + 50
            assert _tokens["cache_read"] >= 200

    def test_add_usage_zero_values(self):
        """Handle empty usage dict."""
        from agent.llm_common import _tokens, _tokens_lock

        with _tokens_lock:
            initial_calls = _tokens["calls"]

        _add_usage({}, 0.0)

        with _tokens_lock:
            assert _tokens["calls"] == initial_calls + 1

    def test_print_token_summary(self, capsys):
        """Print token summary."""
        print_token_summary()

        captured = capsys.readouterr()
        assert "TOKEN USAGE SUMMARY" in captured.out
        assert "API calls" in captured.out
        assert "Estimated cost" in captured.out
