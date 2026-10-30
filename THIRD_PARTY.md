# Dependency and content notice

The repository's existing MIT license is retained, not replaced. Application source and the procedurally rendered native app icon are independently authored; no Readest source/assets, private book contents, generated audio, voice models or third-party fonts are committed. Web documentation screenshots show the separate static demo with original sample fiction and cover artwork. Native screenshots are genuine isolated iOS Simulator captures using original sample books, procedural covers and synthetic cached test audio. Neither set contains private-library content. Web sample narration/importing and native synthetic audio are demonstration fixtures, not evidence of live speech-service availability.

Python packages are installed separately from `requirements.txt`. Reviewed installed metadata:

| Dependency | License |
| --- | --- |
| Flask | BSD |
| Requests | Apache-2.0 |
| Beautiful Soup | MIT |
| EbookLib | GNU AGPLv3 |
| Piper TTS | GPL-3.0-or-later |
| edge-tts | LGPLv3 |
| Gunicorn | MIT |
| Pillow | MIT-CMU |

MIT applies to the original application source, **not** an assertion that the combined runtime is MIT-only. EbookLib is imported by the legacy EPUB export code; Piper is dynamically imported for local synthesis. Their copyleft conditions remain applicable. When conveying a combined application/container or offering relevant network access, review corresponding-source and license/notice obligations; permissive source licensing does not waive them. See [EbookLib's license](https://github.com/aerkalov/ebooklib) and [GNU license compatibility guidance](https://www.gnu.org/licenses/license-compatibility.en.html). No dependency source, binary, container image or model is published by this source-code release.

FFmpeg/espeak-ng and Piper voice models also have separate licenses, which must be respected when distributing containers or models. Do not assume the original-code MIT license relicenses dependencies or content.

The Edge compatibility adapter configures the existing `edge-tts` SDK rather than bundling its implementation. Narration text must be yours to use, and online speech-service terms remain applicable. Synthetic fixtures are used for source-controlled tests.
