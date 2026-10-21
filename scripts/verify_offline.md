# Verify offline operation

Sloth Gist does not download Whisper models while running. Ollama summaries and
model-list requests use the local Ollama service at `127.0.0.1:11434`.

## 1. Download the models once

While internet access is available, download the Ollama model configured in
Sloth Gist:

```console
ollama pull gemma3:4b
```

Download the faster-whisper model you plan to use by initializing it once. For
example, the default `small` model:

```console
python -c "import os; os.environ['HF_HUB_DISABLE_TELEMETRY']='1'; from faster_whisper import WhisperModel; WhisperModel('small', device='cpu', compute_type='int8')"
```

For another Whisper size, replace `small` with `tiny`, `base`, or `medium`.
Model initialization is the only step here that may contact the model host.

## 2. Turn off Wi-Fi and run a digest

With the Ollama service running and Wi-Fi disabled, run the CLI against a folder
containing recordings:

```console
python -m sloth_gist "C:\Users\you\Recordings" --since 24 --whisper small --model gemma3:4b --offline
```

The `--offline` flag is retained for compatibility; downloads are disabled for
all runs. The app sends generation requests only to localhost Ollama. The
transcript itself is held in memory for summarization and is not printed or
written to a file.

## 3. Optionally inspect active connections

On macOS or Linux, find Python's open network connections:

```console
lsof -nP -iTCP -iUDP
```

On Windows, list active TCP connections and process IDs:

```powershell
netstat -ano
```

During summarization, a local connection to `127.0.0.1:11434` is expected. The
web UI also uses the local Sloth Gist server at `127.0.0.1:8765`. No connection
to an external address is needed to run a digest after both models are cached.
