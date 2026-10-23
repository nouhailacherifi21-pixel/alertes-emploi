# 🧪 Alertes emploi LinkedIn

Envoie un email dès qu'une nouvelle offre LinkedIn correspondant au profil est publiée :
ingénieur·e chimiste, laboratoire, traitement des eaux, R&D — Paris / Île-de-France, CDI.

```
Toutes les 15 min (GitHub Actions)
   │
   ├─ 4 recherches LinkedIn : offres publiées dans la dernière heure, Île-de-France
   ├─ Intitulé : un métier (ingénieur·e, chargé·e, chimiste…) + un domaine (chimie, eau, labo, R&D…)
   │            sans stage / alternance / CDD / technicien / senior
   ├─ Annonce : ≤ 3 ans d'expérience demandés, pas de CDD ni d'intérim
   └─ 📧 un email par offre retenue (une seule fois par offre)
```

## Réglages
Tout se règle dans `config.yaml` : recherches, mots de l'intitulé, exclusions, expérience maximale.
Les adresses email ne sont **pas** dans le dépôt : elles sont dans les secrets GitHub
(**Settings → Secrets and variables → Actions**) :

| Secret | Valeur |
|---|---|
| `GMAIL_ADDRESS` | adresse Gmail qui envoie les alertes |
| `GMAIL_APP_PASSWORD` | mot de passe d'application de cette adresse |
| `NOTIFY_EMAIL` | adresse qui reçoit les alertes |

## Tester sans envoyer d'email
```bash
pip install -r requirements.txt
python -m src.alerts --dry-run
```

L'automatisation lit uniquement les pages publiques de LinkedIn, sans connexion à un compte.
