# VoxAero interface fonts

The application serves these WOFF2 assets locally. The fonts have been subset and renamed. Each license is included alongside the derived files.

| File | Upstream source | Coverage |
| --- | --- | --- |
| `Inter-Latin.woff2` | [Inter 4.1](https://github.com/rsms/inter/releases/tag/v4.1) | Latin interface text and symbols |
| `JetBrainsMono-Latin.woff2` | [JetBrains Mono 2.304](https://github.com/JetBrains/JetBrainsMono/tree/v2.304) | Call signs, readouts, time and coordinates |
| `NotoSansSC-Interface.woff2` | [Noto Sans SC, sourced from Noto CJK Sans 2.004](https://github.com/google/fonts/tree/main/ofl/notosanssc) | Han characters and punctuation used in the application, backend messages and scene data |

The CSS includes native sans-serif fallbacks for characters outside the bundled subsets.
