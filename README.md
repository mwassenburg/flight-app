# Ryanair deal scanner

Zoekt goedkope Ryanair-retourtjes (twee losse enkele reizen) voor korte trips van 1 t/m 7 dagen
vanaf AMS, RTM, EIN, MST en NRN. Alles draait in GitHub Actions; het dashboard staat op GitHub Pages.

## Hoe het werkt

1. `scan.py` vraagt bij Ryanair per thuisluchthaven de bestemmingen op, en daarna per route en per
   maand de goedkoopste prijs per dag, in beide richtingen. Ryanair vliegt nu alleen vanaf EIN, NRN
   en AMS; RTM en MST worden automatisch overgeslagen en doen vanzelf mee zodra Ryanair er gaat vliegen.
2. Heen- en terugvluchten worden gecombineerd: elk onder €100, terug 1–7 dagen later, totaal onder €200.
   Terug mag naar een andere thuisluchthaven (open jaw). Sterren: ★★ onder €100, ★ onder €150.
3. Het resultaat komt in `data/deals.json`.
4. De workflow `.github/workflows/scan.yml` draait dit elke 6 uur en commit het bestand als het veranderd is.
5. `index.html` leest `data/deals.json` in en toont de deals met filters.
   Deals met vertrek op vrijdag vanaf 12:00 en terug op zondag vanaf 17:00 zijn groen en hebben het
   label en filter **IDEAAL WEEKEND** (de tijdgrenzen staan bovenaan het script in `index.html`).

Een scan duurt ruim 20 minuten (ongeveer 1.250 requests, maximaal 1 per seconde).

### Formaat van deals.json

Om het bestand klein te houden staat elke deal als rij op één regel; `columns` geeft de kolomnamen:
`from, dest, back, out, ret, days, price_out, price_back, total, stars, open_jaw`.
Stad en land staan in `destinations`, het tijdstip van de scan in `scanned_at`. De boekingslink
bouwt het dashboard uit `book_url` plus de velden van de deal. Tijden zijn lokale tijd op de luchthaven.

## Eenmalig instellen

1. Push deze map naar een GitHub-repository. Voor gratis GitHub Pages moet die **public** zijn.
2. **Pages aanzetten:** Settings → Pages → Source: *Deploy from a branch* → Branch: `main`, map `/ (root)` → Save.
   Na een minuut staat het dashboard op `https://<gebruikersnaam>.github.io/<repo>/`.
3. Controleer onder Settings → Actions → General → Workflow permissions dat *Read and write permissions* aan staat.

## Handmatig een scan starten

Actions → **Ryanair scan** → **Run workflow** → Run workflow.

## Als Ryanair blokkeert

De scan stopt dan met een rode fout ("Ryanair blokkeert de scanner") en laat `deals.json` ongemoeid;
het dashboard toont de vorige scan en waarschuwt als die ouder is dan een dag. In de log staan de
HTTP-statussen en alternatieven (later opnieuw, self-hosted runner, proxy, of elders scannen).

## Lokaal testen (optioneel)

```bash
pip install -r requirements.txt
python scan.py --max-routes 3
python -m http.server 8123
```

Instellingen zoals prijsgrenzen en thuisluchthavens staan bovenaan `scan.py`.
