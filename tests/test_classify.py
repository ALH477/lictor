"""Every row of repl.classify's table, plus eval_image's exec-all-but-last
rule and the !! block / leading-space escape."""

from __future__ import annotations

from lictor.repl import Command, ImageForm, Submission, classify, eval_image


def test_blank_line_is_none() -> None:
    assert classify("") is None
    assert classify("   ") is None


def test_plain_text_is_prose_submission() -> None:
    result = classify("hello there")
    assert result == Submission("hello there")


def test_leading_space_escapes_to_prose() -> None:
    # Without the leading space these would be a Command / ImageForm.
    assert classify(" /not-a-command") == Submission("/not-a-command")
    assert classify(" !not-an-image-form") == Submission("!not-an-image-form")
    assert classify(" plain text") == Submission("plain text")


def test_slash_command_no_args() -> None:
    result = classify("/help")
    assert result == Command("help", "")


def test_slash_command_with_args() -> None:
    result = classify("/model claude-sonnet-5")
    assert result == Command("model", "claude-sonnet-5")


def test_slash_command_args_keep_internal_spaces() -> None:
    result = classify("/resume some-conversation-id")
    assert result == Command("resume", "some-conversation-id")


def test_bang_line_is_image_form() -> None:
    result = classify("!1 + 1")
    assert result == ImageForm("1 + 1")


def test_bare_bang_is_image_form_with_empty_src() -> None:
    result = classify("!")
    assert result == ImageForm("")


def test_multiline_block_is_image_form() -> None:
    # The REPL loop assembles a !! ... !! block's body (delimiters already
    # stripped) and joins it with "\n" before calling classify(); the
    # embedded newline is what marks it as a block rather than a line.
    body = "x = 1\nx + 1"
    result = classify(body)
    assert result == ImageForm(body)


def test_multiline_block_leading_space_escapes_to_prose() -> None:
    result = classify(" literal\ntext")
    assert result == Submission("literal\ntext")


def test_eval_image_returns_last_expression_value() -> None:
    namespace: dict = {}
    assert eval_image("1 + 1", namespace) == 2


def test_eval_image_execs_all_but_last_statement() -> None:
    namespace: dict = {}
    result = eval_image("x = 1\ny = x + 1\ny + 1", namespace)
    assert result == 3
    assert namespace["x"] == 1
    assert namespace["y"] == 2


def test_eval_image_last_statement_not_expression_returns_none() -> None:
    namespace: dict = {}
    result = eval_image("x = 1", namespace)
    assert result is None
    assert namespace["x"] == 1


def test_eval_image_uses_namespace_functions() -> None:
    calls: list[tuple[str, str | None]] = []

    def fake_prompt(text: str, to: str | None = None) -> Submission:
        calls.append((text, to))
        return Submission(text, to=to, source="image")

    namespace = {"prompt": fake_prompt}
    result = eval_image('prompt("hello", to="lexer")', namespace)
    assert result == Submission("hello", to="lexer", source="image")
    assert calls == [("hello", "lexer")]
