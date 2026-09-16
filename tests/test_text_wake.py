import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from main import JarvisLive


class TextWakeTests(unittest.TestCase):
    def test_typed_command_wakes_and_reaches_live_session(self):
        session = SimpleNamespace(send_client_content=AsyncMock())
        jarvis = SimpleNamespace(
            _loop=object(), session=session, _wake_enabled=True, _awake=False,
            wake=Mock(),
        )
        with patch("main.asyncio.run_coroutine_threadsafe", side_effect=lambda coro, _loop: coro.close()):
            JarvisLive._on_text_command(jarvis, "Riassumi le email")

        jarvis.wake.assert_called_once_with(reason="typed command")
        session.send_client_content.assert_called_once()


if __name__ == "__main__":
    unittest.main()
