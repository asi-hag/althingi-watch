# Alþingi vöktun

Sjálfvirk vöktun á þingskjölum, umsagnarbeiðnum og nefndarfundum á Alþingi, með lykilorðasíun fyrir vinnumarkaðsmál.

## Uppbygging

```
scripts/fetch.py          # Sækir gögn úr RSS og HTML
scripts/requirements.txt  # Python dependencies
site/index.html           # Vefviðmót með síum og töflu
site/data/                # JSON gögn (sjálfvirkt uppfærð)
.github/workflows/        # GitHub Actions – keyrir daglega
```

## Keyra staðbundið

```bash
pip install -r scripts/requirements.txt
python scripts/fetch.py
# Opnaðu site/index.html í vafra (þarf HTTP server fyrir fetch)
python -m http.server 8000 -d site
```

## GitHub Pages

Til að birta síðuna á GitHub Pages, farðu í **Settings → Pages** og veldu `main` branch og `/site` möppu.
