# 🧪 Alertes emploi LinkedIn

Envoie un email dès qu'une nouvelle offre LinkedIn correspondant au profil est publiée :
ingénieur·e chimiste, laboratoire, traitement des eaux, R&D — Paris / Île-de-France, CDI.

```
Toutes les 15 min (GitHub Actions)
   │
   ├─ 5 recherches LinkedIn : offres publiées dans la dernière heure, Île-de-France
   ├─ Exclusions sur l'intitulé : stage, alternance, CDD, technicien, senior…
   ├─ Claude Haiku lit chaque annonce : missions dans les domaines visés ? poste d'ingénieure ?
   │  ≤ 3 ans d'expérience ? CDI ? Paris / Île-de-France ? + note d'adéquation avec le profil
   └─ 📧 un email par offre retenue, les mieux notées d'abord (une seule fois par offre)
```

## Réglages
Tout se règle dans `config.yaml` : recherches, exclusions, domaines visés, profil (anonyme) et préférences.
Les adresses email ne sont **pas** dans le dépôt : elles sont dans les secrets GitHub
(**Settings → Secrets and variables → Actions**) :

| Secret | Valeur |
|---|---|
| `GMAIL_ADDRESS` | adresse Gmail qui envoie les alertes |
| `GMAIL_APP_PASSWORD` | mot de passe d'application de cette adresse |
| `NOTIFY_EMAIL` | adresse qui reçoit les alertes |
| `ANTHROPIC_API_KEY` | clé API Claude (tri des offres, ≈ 1,5 à 3 $/mois) |

## Tester sans envoyer d'email
```bash
pip install -r requirements.txt
python -m src.alerts --dry-run
```

L'automatisation lit uniquement les pages publiques de LinkedIn, sans connexion à un compte.
