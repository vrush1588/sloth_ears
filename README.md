# Sloth Ears 🦥

**Local audio and video digests, at a gentler pace.** Sloth Ears transcribes
speech with faster-whisper, then turns it into a short summary and action items
with a local Ollama model. Video frames are not analyzed.

> **Screenshot:** _Add a screenshot of the Sloth Ears app here._

## Why open models?

- **Works offline:** download models once, then keep your recordings on-device.
- **Private:** no cloud transcription or summaries; transcripts are not saved.
- **Swappable:** choose a Whisper size and any model available in local Ollama.
- **Free to run:** open models have no per-minute or per-summary API fees. Your
  computer supplies the compute.

## Quick start

1. [Install Ollama](https://ollama.com/download) and pull the default summary
   model:

   ```console
   ollama pull llama3.2:3b
   ```

2. Install the Python dependency:

   ```console
   python -m pip install -r requirements.txt
   ```

3. Start the web app:

   ```console
   python -m sloth_ears.server
   ```

   Open <http://127.0.0.1:8765>. On first use, the selected faster-whisper model
   must already be downloaded and cached. For example, initialize `small` once
   while online:

   ```console
   python -c "from faster_whisper import WhisperModel; WhisperModel('small', device='cpu', compute_type='int8')"
   ```

## CLI

Run the CLI on a folder of recordings:

```console
python -m sloth_ears "C:\Users\you\Recordings" --since 24 --whisper small --model llama3.2:3b --language en
```

Use `--language hi` for Hindi, `--language mr` for Marathi, or omit the option
for automatic detection. `--since` is in hours; the web app also offers a
7-day or all-recordings range. The `--offline` option is retained for
compatibility; model downloads are disabled during runs.

Supported audio formats: `.opus`, `.ogg`, `.m4a`, `.mp3`, `.wav`, `.aac`,
`.amr`, `.flac`, `.wma`, `.m4b`, `.3gp`. Supported video containers:
`.mp4`, `.mkv`, `.mov`, `.webm`, `.avi`, `.m4v`.

Digests are saved as Markdown and JSON under
`~/Documents/SlothEars/Digests/`. Raw transcripts are not saved.

## Text and key points

Sloth Ears can also digest text, with no Whisper step. In the web app, open the
**Text** tab on the Drop Zone, then paste text (up to 200,000 characters) or
open a `.txt` / `.md` file. The file is read in your browser and sent only to
the local server on `127.0.0.1`.

Use the **Output** menu to choose the format. It works for both recordings and
text:

| Output     | What you get                                          |
| ---------- | ----------------------------------------------------- |
| One line   | A single short summary line (the default)             |
| Key points | 3 to 6 short key points; long text is chunked and the points merged, de-duplicated and capped at 8 |
| Both       | The summary line plus the key points                  |

From the CLI, pass `--text` with a `.txt`/`.md` file or a folder of them, and
`--style line|points|both`:

```console
python -m sloth_ears "C:\Users\you\Notes\meeting.md" --text --style both --model gemma3:4b
```

The API accepts `POST /api/run` with `"mode": "text"` and either `"text"` (with
an optional `"name"`) or `"path"`, plus `"style"`.

**Privacy:** the input text is kept in memory only. Only the digest is saved
(the summary, key points and to-dos). Summaries are written in your own words:
any line that copies four or more words straight from the input is dropped.

**Language:** summaries and key points are written in the input language. Pick
Hindi or Marathi under **Language**, or leave it on Auto-detect. Auto-detect
tells Hindi and Marathi apart from common words, and asks the model to keep any
other language as it is.

**Choosing a model:** the model menu in Settings lists every model your local
Ollama reports. Models whose names start with `gemma` are listed first, under
**Recommended for multilingual**, because they handle Hindi and Marathi better
than most small models:

```console
ollama pull gemma3:4b
```

## Limits

- Models need to be downloaded once before they can be used offline.
- Small Whisper models are faster, but may be less accurate.
- Hindi and Marathi transcription quality varies with the audio and model.
- Sloth Ears summarizes **speech only**; video frames and visual content are
  ignored.
- Local models use your computer’s memory and processing power.

## Verify offline use

Pull the models first, turn off Wi-Fi, and run a summary. Optionally inspect
connections with `lsof` (macOS/Linux) or `netstat -ano` (Windows). See
[scripts/verify_offline.md](./scripts/verify_offline.md) for step-by-step
instructions.
