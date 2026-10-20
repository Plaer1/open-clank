# Bundled font identity and notices

Inspected directly from the shipped font name tables on October 4, 2026; filenames alone are not provenance. Font bytes are unchanged. [Asset fingerprints](BUNDLED-COMPONENTS.md#asset-fingerprints) identify the inspected variants.

| Files under static/fonts/ | Embedded identity/version | License evidence |
| --- | --- | --- |
| FiraCode-{Light,Regular,SemiBold}.woff2 | Fira Code 6.002 | Embedded OFL-1.1; Fira Code Project Authors 2014–2021 |
| Inter-{Regular,Medium,SemiBold}.woff2 | Inter 4.001; git-9221beed3 | Embedded OFL-1.1; Inter Project Authors 2016 |
| Fredoka-Variable.woff2 | Fredoka 2.001; weight 300–700, width 75–125 | Embedded OFL-1.1; Fredoka Project Authors 2016, Milena B. Brandão and Ben Nathan |
| ComicNeue-Regular.woff2 | Comic Neue 2.003 | Embedded OFL-1.1; Craig Rozynski; [retained text](ComicNeue-OFL.txt) |
| OpenDyslexic-{Regular,Bold}.woff2 | OpenDyslexic 0.920 | Embedded full OFL-1.1 and reserved-name notice; Abbie Gonzalez; [retained text](OpenDyslexic-OFL.txt) |
| LigaComicMono-Regular.woff2 | Liga Comic Mono 0.1.1 | Comic Mono source URL plus Ilya Skriblovsky/Fira Code ligature copyright; see unresolved chain below |
| custom/GohuFont.ttf | Untitled1 Regular 001.000; copyright 2025 Unknown | No embedded license/source; historical Gohu/WTFPL attribution is unverified for these bytes |

Fira Code upstream: https://github.com/tonsky/FiraCode. Inter: https://github.com/rsms/inter. Fredoka identifies https://github.com/hafontia/Fredoka-One in its copyright metadata; current Google Fonts sources are at https://github.com/google/fonts/tree/main/ofl/fredoka. Exact original download URLs and transformations are not recorded for these WOFF2s, but their embedded identity and OFL license statements are explicit.

## Liga Comic Mono qualification

The exact bundled font contains this copyright field:

```text
https://github.com/dtinth/comic-mono-font/blob/master/LICENSE
Programming ligatures added by Ilya Skriblovsky from FiraCode
FiraCode Copyright (c) 2015 by Nikita Prokopov
```

The [Comic Mono upstream notice](https://github.com/dtinth/comic-mono-font/blob/master/LICENSE) credits Shannon Miwa (2018) and dtinth (2019), under MIT. Its base derives from Comic Shanns. Fira Code uses OFL-1.1. The related [wayou fork](https://github.com/wayou/comic-mono-font) documents ligaturization, but the shipped WOFF2 has not been matched to an immutable fork release or transformation recipe. Its exact combined-font notice/license disposition remains unresolved; retain both upstream notices and the embedded ligature attribution, without describing the combined font as wholly MIT or wholly OFL. No Nerd Font variant is claimed.

## File named GohuFont.ttf

The prior acknowledgments named Hugo Chargois and WTFPL for GohuFont. The actual file name table says `Untitled1`, `Version 001.000`, `Copyright (c) 2025, Unknown`, with no license/source fields. Those bytes have not been authenticated as upstream [GohuFont](https://font.gohu.org/). The historical credit is retained here as unverified, not as a license grant. The file remains present; resolving its acquisition or conversion chain is an open provenance item.

## FiraCode embedded copyright and license statement

```text
Copyright 2014-2021 The Fira Code Project Authors (https://github.com/tonsky/FiraCode)
This Font Software is licensed under the SIL Open Font License, Version 1.1. This license is available with a FAQ at: http://scripts.sil.org/OFL
```

## Inter- embedded copyright and license statement

```text
Copyright 2016 The Inter Project Authors
This Font Software is licensed under the SIL Open Font License, Version 1.1. This license is available with a FAQ at: http://scripts.sil.org/OFL
```

## Fredoka embedded copyright and license statement

```text
Copyright 2016 The Fredoka Project Authors (https://github.com/hafontia/Fredoka-One)
This Font Software is licensed under the SIL Open Font License, Version 1.1. This license is available with a FAQ at: http://scripts.sil.org/OFL
```

## SIL Open Font License 1.1 (applies to the identified OFL fonts above)

```text
-----------------------------------------------------------
SIL OPEN FONT LICENSE Version 1.1 - 26 February 2007
-----------------------------------------------------------

PREAMBLE
The goals of the Open Font License (OFL) are to stimulate worldwide
development of collaborative font projects, to support the font creation
efforts of academic and linguistic communities, and to provide a free and
open framework in which fonts may be shared and improved in partnership
with others.

The OFL allows the licensed fonts to be used, studied, modified and
redistributed freely as long as they are not sold by themselves. The
fonts, including any derivative works, can be bundled, embedded, 
redistributed and/or sold with any software provided that any reserved
names are not used by derivative works. The fonts and derivatives,
however, cannot be released under any other type of license. The
requirement for fonts to remain under this license does not apply
to any document created using the fonts or their derivatives.

DEFINITIONS
"Font Software" refers to the set of files released by the Copyright
Holder(s) under this license and clearly marked as such. This may
include source files, build scripts and documentation.

"Reserved Font Name" refers to any names specified as such after the
copyright statement(s).

"Original Version" refers to the collection of Font Software components as
distributed by the Copyright Holder(s).

"Modified Version" refers to any derivative made by adding to, deleting,
or substituting -- in part or in whole -- any of the components of the
Original Version, by changing formats or by porting the Font Software to a
new environment.

"Author" refers to any designer, engineer, programmer, technical
writer or other person who contributed to the Font Software.

PERMISSION & CONDITIONS
Permission is hereby granted, free of charge, to any person obtaining
a copy of the Font Software, to use, study, copy, merge, embed, modify,
redistribute, and sell modified and unmodified copies of the Font
Software, subject to the following conditions:

1) Neither the Font Software nor any of its individual components,
in Original or Modified Versions, may be sold by itself.

2) Original or Modified Versions of the Font Software may be bundled,
redistributed and/or sold with any software, provided that each copy
contains the above copyright notice and this license. These can be
included either as stand-alone text files, human-readable headers or
in the appropriate machine-readable metadata fields within text or
binary files as long as those fields can be easily viewed by the user.

3) No Modified Version of the Font Software may use the Reserved Font
Name(s) unless explicit written permission is granted by the corresponding
Copyright Holder. This restriction only applies to the primary font name as
presented to the users.

4) The name(s) of the Copyright Holder(s) or the Author(s) of the Font
Software shall not be used to promote, endorse or advertise any
Modified Version, except to acknowledge the contribution(s) of the
Copyright Holder(s) and the Author(s) or with their explicit written
permission.

5) The Font Software, modified or unmodified, in part or in whole,
must be distributed entirely under this license, and must not be
distributed under any other license. The requirement for fonts to
remain under this license does not apply to any document created
using the Font Software.

TERMINATION
This license becomes null and void if any of the above conditions are
not met.

DISCLAIMER
THE FONT SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO ANY WARRANTIES OF
MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT
OF COPYRIGHT, PATENT, TRADEMARK, OR OTHER RIGHT. IN NO EVENT SHALL THE
COPYRIGHT HOLDER BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY,
INCLUDING ANY GENERAL, SPECIAL, INDIRECT, INCIDENTAL, OR CONSEQUENTIAL
DAMAGES, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING
FROM, OUT OF THE USE OR INABILITY TO USE THE FONT SOFTWARE OR FROM
OTHER DEALINGS IN THE FONT SOFTWARE.
```

## Comic Mono base notice (retrieved October 4, 2026; exact Liga release unresolved)

```text
MIT License

Original work Copyright (c) 2018 Shannon Miwa
Modified work Copyright (c) 2019 dtinth

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```
