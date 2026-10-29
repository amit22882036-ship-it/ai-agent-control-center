# Atlas fonts

The Atlas interface uses locally hosted font files, so it makes no request to a font service at
runtime. All three families are licensed under the SIL Open Font License 1.1; each license text is
kept next to its files.

| Family | Files | License | Upstream |
| --- | --- | --- | --- |
| Hanken Grotesk (variable, weights 400–700) | `hanken-grotesk-latin.woff2`, `hanken-grotesk-latin-ext.woff2` | [OFL 1.1](hanken-grotesk-OFL.txt) | github.com/marcologous/hanken-grotesk |
| Heebo (variable, weights 400–700, Hebrew subset) | `heebo-hebrew.woff2` | [OFL 1.1](heebo-OFL.txt) | github.com/OdedEzer/heebo |
| JetBrains Mono (variable, weights 400–500) | `jetbrains-mono-latin.woff2`, `jetbrains-mono-latin-ext.woff2` | [OFL 1.1](jetbrains-mono-OFL.txt) | github.com/JetBrains/JetBrainsMono |

The WOFF2 subsets are the ones Google Fonts serves for these families (Hanken Grotesk v12, Heebo
v28, JetBrains Mono v24), downloaded unmodified. The license texts come from the `google/fonts`
repository (`ofl/<family>/OFL.txt`). The `@font-face` rules in `../styles/tokens.css` register them
under the names `Atlas Hanken Grotesk`, `Atlas Heebo` and `Atlas JetBrains Mono`, with the upstream
`unicode-range` values, so a browser downloads only the subsets a page needs and the classic
interface's fonts are unaffected. Heebo supplies Hebrew text only; Latin text uses Hanken Grotesk.
