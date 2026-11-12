// Every catalogue the page carries, by locale. en-GB is the source; the rest translate it.
// To add a locale, add its file here (see CONTRIBUTING.md).

import ar from './ar.ts';
import de from './de.ts';
import type { Catalogue } from './en-GB.ts';
import enGB from './en-GB.ts';
import enUS from './en-US.ts';
import es from './es.ts';
import fr from './fr.ts';
import hi from './hi.ts';
import it from './it.ts';
import ja from './ja.ts';
import ko from './ko.ts';
import nl from './nl.ts';
import pl from './pl.ts';
import ptBR from './pt-BR.ts';
import zhHans from './zh-Hans.ts';

export const CATALOGUES: Record<string, Catalogue> = {
    'en-GB': enGB,
    'en-US': enUS,
    ar: ar,
    de: de,
    es: es,
    fr: fr,
    hi: hi,
    it: it,
    ja: ja,
    ko: ko,
    nl: nl,
    pl: pl,
    'pt-BR': ptBR,
    'zh-Hans': zhHans,
};
