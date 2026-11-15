# WeTransfer Ophalen

Een Windows-app die WeTransfer-links automatisch ophaalt en daarna een berichtje
naar Slack stuurt. Plak een link (of laat hem in de wachtmap vallen), en de app
regelt de rest — ook als een download halverwege afbreekt.

![De app tijdens het ophalen](docs/screenshot-ophalen.png)

## Wat de app doet

- **Links ophalen van elke WeTransfer-vorm**: `https://we.tl/t-…`, `https://wetransfer.com/downloads/…`
  (met of zonder ontvanger-id) en een al ondertekende `download.wetransfer.com`-link.
- **Automatisch oppakken**: zet een opgeslagen e-mail of tekstbestand met een link in de
  wachtmap en de app begint vanzelf. Verwerkte bestanden gaan naar de submap `verwerkt`.
- **Hervatten**: bij een afgebroken verbinding of een verlopen downloadlink vraagt de app
  een nieuwe link aan en gaat verder met een `Range`-verzoek. Halve downloads blijven als
  `.wetransfer.part` staan, dus na een herstart gaat hij verder waar hij gebleven was.
- **Slack-melding** per opgehaalde transfer, plus een samenvatting als de hele wachtrij
  klaar is. Bij een fout krijg je de reden in het bericht (bijvoorbeeld "Deze transfer is verlopen").
- **Wachtwoord-beveiligde transfers**: vul het wachtwoord in bij het toevoegen van de link.
- **Nette bestandsnamen**: de naam die WeTransfer zelf meegeeft, ontdaan van tekens die
  Windows niet accepteert; een bestaand bestand wordt nooit overschreven (`Archiv (2).zip`).
- **Logboek** in `%APPDATA%\WeTransfer Ophalen\app.log`, plus een venster met de laatste regels.
- **Start mee met Windows** (optioneel) en een geluidje als een download klaar is.

## Installeren

1. Download `WeTransferOphalen-Windows.zip` en pak hem uit (bijvoorbeeld naar
   `C:\Program Files\WeTransfer Ophalen` of gewoon in je Documenten).
2. Start `WeTransfer Ophalen.exe`.
3. **Laat `WeTransfer Ophalen.exe.config` naast de .exe staan.** Zonder dat bestand weigert
   Windows de app te starten zodra hij uit een gedownloade ZIP komt (het "Mark of the Web").
   Windows zal bij de eerste start om bevestiging vragen (SmartScreen → *Meer informatie* →
   *Toch uitvoeren*), omdat de app niet ondertekend is.

Er is geen installatieprogramma en er is niets nodig van Python of .NET: alles zit in de map.

## Slack-melding instellen

1. Ga naar <https://api.slack.com/apps> → **Create New App** → **From scratch**.
2. Kies je werkruimte en een naam, bijvoorbeeld *WeTransfer Ophalen*.
3. Klik links op **Incoming Webhooks** en zet de schakelaar aan.
4. Klik op **Add New Webhook to Workspace**, kies het kanaal (een DM aan jezelf werkt ook)
   en geef toestemming.
5. Kopieer de webhook-URL, plak hem in de app bij **Slack-melding** en klik op
   **Testbericht sturen**.

De webhook staat in `%APPDATA%\WeTransfer Ophalen\settings.json` en gaat nergens anders heen
dan naar Slack.

## Hoe het werkt

WeTransfer heeft geen openbare API om andermans transfers op te halen. De app doet daarom
precies de twee verzoeken die de knop *Downloaden* op wetransfer.com zelf doet:

1. `POST https://wetransfer.com/api/v4/transfers/<id>/prepare-download` met het security-hash
   uit de link → status van de transfer (klaar, verlopen, wachtwoord), de aanbevolen
   bestandsnaam en de grootte.
2. `POST https://wetransfer.com/api/v4/transfers/<id>/download` → `direct_link`, een
   ondertekende link naar `download.wetransfer.com` die ongeveer tien minuten geldig blijft.

Daarna downloadt de app die link in brokken van 256 kB, met voortgang, snelheid en
tijdsindicatie in het venster.

## Ontwikkelen

```bash
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v        # 55 tests, geen internet nodig
python main.py                                  # de app starten vanaf de broncode
python main.py --selftest rapport.json --core-only   # de kern testen (zonder venster)
```

De tests en de zelftest draaien tegen `wtdl/mock.py`: een lokale nabootser van WeTransfer en
Slack. Daardoor is de uitslag niet afhankelijk van internet of van een bestaande transfer.

Windows-build (werkt lokaal precies als in CI):

```bat
build_windows.bat
```

Dat maakt `dist\WeTransfer Ophalen\`, inclusief het pictogram uit `branding/app.ico` en de
`.exe.config`. CI (`.github/workflows/windows-build.yml`) draait de tests, bouwt de app,
draait de **zelftest van de gebouwde .exe** vanuit een map die als "gedownload" is gemarkeerd
en publiceert de ZIP als artifact en als GitHub-release.

Wat die zelftest bewijst, met de gebouwde .exe:

- een echte download tegen de nabootser, met controle van de sha256 van de inhoud;
- hervatten vanaf een half bestand, met een `Range`-verzoek vanaf de juiste byte;
- de foutafhandeling bij een verlopen transfer;
- links uit een bestand in de wachtmap halen en het bestand naar `verwerkt` verplaatsen;
- de wachtrij met Slack-meldingen (één per transfer, plus de samenvatting);
- dat het venster opent, dat de pagina zijn eigen JavaScript heeft geladen en dat de
  mapkiezer op de GUI-thread wordt geopend (WinForms weigert dat anders, en dan doet de
  knop niets).

## Beperkingen

- Alleen Windows (de app gebruikt pywebview met de Edge/WebView2-engine; Windows 10 en 11
  hebben die standaard).
- De app leunt op dezelfde niet-openbare endpoints als de website. Verandert WeTransfer die,
  dan moet de app mee. Een link die niet meer bestaat of verlopen is, levert een duidelijke
  melding op in plaats van een crash.
- Geen automatische updates: een nieuwe versie is een nieuwe ZIP.
- Bij een transfer met meerdere bestanden levert WeTransfer één ZIP; dat is precies wat de
  app binnenhaalt.

## Licentie

Alle rechten voorbehouden. Dit is eigen werk van Werdens Media; er is **geen code van derden**
in overgenomen. De functionele inspiratie voor het omgaan met WeTransfer-links komt van het
open-sourceproject [transferwee](https://github.com/iamleot/transferwee), en de gebruikte
endpoints komen uit de JavaScript-bundel van wetransfer.com zelf. Er is geen regel code uit
die projecten overgenomen.
