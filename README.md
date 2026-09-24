# Mark-LIV

MARK LIV is a cross-platform JARVIS-style assistant with an optional local **Hey Jarvis** wake word.

## Run

```bash
python setup.py
python main.py
```

The wake-word engine is optional. Open the app's **⚙ settings drawer** and choose **WAKE WORD: DOWNLOAD**. The installer puts the model in the openwakeword package directory and runs native wake-word setup in a child process, so a broken ONNX runtime cannot close the main window.

If the settings drawer or download still has a problem, run the dependency diagnostic from the repository root:

```bash
python check_wake_word.py
```

The diagnostic checks NumPy, ONNX Runtime, and openwakeword in isolated subprocesses and reports native crashes separately. It does not require an API key or network access for the basic checks.

Your API keys and runtime memory are intentionally ignored by Git. Never commit `config/api_keys.json` or personal data from `memory/`.
