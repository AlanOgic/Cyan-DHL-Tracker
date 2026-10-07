# Codes de suivi DHL

> Page générée par `dhl_codes_doc.py` depuis le code du tracker : ne pas la modifier à la main. Après un changement des codes d'alerte, relancer `python dhl_codes_doc.py` ; `tests/test_dhl_codes_doc.py` échoue tant que la page n'est pas à jour.

Ce que DHL renvoie pour chacun de nos envois Express, comment lire ses codes, et lesquels ouvrent un ticket dans le Helpdesk.

Sources : API DHL Shipment Tracking – Unified v1.5.8 (`API/track_v1.5.8.yaml`), liste officielle des codes Express DHL (`API/status_1.csv`), réponses réelles de nos envois (octobre 2026). Tous nos transporteurs DHL dans Odoo sont des produits Express.

**Sommaire :** [Un événement, trois niveaux](#un-événement-trois-niveaux) · [Les 40 codes événement Express](#les-40-codes-événement-express) · [Quand un ticket est ouvert](#quand-un-ticket-est-ouvert) · [Ce que l'API renvoie pour nos envois](#ce-que-lapi-renvoie-pour-nos-envois) · [Limites à connaître](#limites-à-connaître)

## Un événement, trois niveaux

Chaque événement DHL porte trois informations superposées. Exemple, avec un numéro fictif : l'envoi `1234567890` en EXPRESS 12:00, de Bruxelles vers l'Ohio, le 2026-10-06 à 05:51 (−04:00) à CINCINNATI HUB, Ohio, USA.

| Niveau | Champ API | Exemple | À quoi il sert |
|---|---|---|---|
| 1 · Statut global | `status.statusCode` | `transit` | 5 valeurs communes à tous les services DHL. **Seule information utilisée pour décider qu'un envoi est livré.** |
| 2 · Code Express | `events[].status` | `DF` | Depart facility. 40 codes de 2 lettres qui disent ce qui s'est passé, et donc s'il faut agir. |
| 3 · Description | `description` | Shipment has departed from a DHL facility CINCINNATI HUB - USA | Texte libre, en français avec `language=fr` : « L'envoi a quitté un site DHL ». Jamais utilisé pour décider : « The shipment could not be delivered » contient aussi le mot « delivered ». |

### Les 5 statuts globaux

| Statut | Signification |
|---|---|
| `pre-transit` | Étiquette créée, pas encore remis à DHL. |
| `transit` | En route, y compris en douane ou en attente. |
| `delivered` | Livré. L'envoi est marqué livré dans Odoo. |
| `failure` | Échec : non livré, retour, perte. |
| `unknown` | DHL ne sait pas encore. |

### Les 4 codes numériques Express

Ils figurent sur le statut global de l'envoi, pas sur les événements.

| Code | Statut global |
|---|---|
| `104` | `pre-transit` |
| `102` | `transit` |
| `101` | `delivered` |
| `103` | `failure` |

## Les 40 codes événement Express

19 codes signalent un problème : 16 ouvrent un ticket, et `MD` `NH` `OH` sont seulement signalés sur Mattermost, car DHL les règle le plus souvent seul (réglage par défaut, modifiable avec `HELPDESK_SKIP_CODES`). Les autres décrivent le parcours normal du colis.

_sans date estimée_ : DHL ne donne pas de date de livraison estimée avec cet événement (« no EDD » dans sa liste).

### Parcours normal · 13 codes

| Code | Libellé DHL | Ce que ça veut dire | Alerte |
|---|---|---|---|
| `PU` | Shipment pick up | Colis enlevé chez l'expéditeur. | — |
| `SA` | Shipment acceptance | Pris en charge par DHL. | — |
| `PL` | Processed at location | Traité dans un site DHL. | — |
| `DF` | Depart facility | A quitté un site DHL. | — |
| `AF` | Arrived facility | Arrivé dans un site DHL. | — |
| `AR` | Arrival in delivery facility | Arrivé à l'agence qui va livrer. | — |
| `WC` | With delivering courier | En tournée de livraison. | — |
| `OK` | Delivery | Livré. _(sans date estimée)_ | — |
| `TR` | Record of transfer | Transfert enregistré entre deux réseaux. | — |
| `SM` | Scheduled for movement | Planifié sur un prochain acheminement. _(depuis juin 2024)_ | — |
| `FD` | Forward destination (DD's expected) | Réacheminé vers une autre destination, livraison toujours attendue. | — |
| `AD` | Agreed delivery | Date ou lieu de livraison convenu avec le destinataire. | — |
| `SC` | Service changed | Produit ou service DHL modifié. | — |

### Douane & paiement · 9 codes

| Code | Libellé DHL | Ce que ça veut dire | Alerte |
|---|---|---|---|
| `IC` | In clearance processing | En cours de dédouanement. | — |
| `RR` | Response received | Réponse reçue, souvent de la douane. | — |
| `CR` | Clearance release | Dédouané. | — |
| `BR` | Broker release | Libéré par le transitaire. _(sans date estimée)_ | — |
| `BN` | Customer broker notified | Transitaire du destinataire prévenu. | — |
| `CD` | Controllable clearance delay | Retard de douane que l'on peut débloquer : document ou information manquante. | **Ticket** · Haute |
| `UD` | Uncontrollable clearance delay | Retard de douane hors de notre main : contrôle, inspection. | **Ticket** · Haute |
| `HP` | Held for payment | Bloqué jusqu'au paiement des droits et taxes. | **Ticket** · Haute |
| `PY` | Payment | Paiement reçu. _(depuis juin 2024)_ | — |

### Échec de livraison & adresse · 7 codes

| Code | Libellé DHL | Ce que ça veut dire | Alerte |
|---|---|---|---|
| `ND` | Not delivered | Non livré. | **Ticket** · Haute |
| `NH` | Not home | Destinataire absent. _(sans date estimée)_ | Mattermost |
| `MD` | Missed delivery cycle | Tournée de livraison manquée. | Mattermost |
| `CA` | Closed on arrival | Destinataire fermé au passage du livreur. _(sans date estimée)_ | **Ticket** · Haute |
| `CC` | Awaiting cnee collection | À retirer par le destinataire en agence ou point relais. _(sans date estimée)_ | **Ticket** · Haute |
| `BA` | Bad address | Adresse incorrecte ou incomplète. | **Ticket** · Haute |
| `CM` | Customer moved | Le destinataire a déménagé. | **Ticket** · Haute |

### Refus, retour, dommage · 5 codes

| Code | Libellé DHL | Ce que ça veut dire | Alerte |
|---|---|---|---|
| `RD` | Refused delivery | Refusé par le destinataire. _(sans date estimée)_ | **Ticket** · Urgente |
| `RT` | Returned to consignor | Retourné à l'expéditeur, donc à nous. _(sans date estimée)_ | **Ticket** · Urgente |
| `DD` | Delivered damaged | Livré endommagé. _(sans date estimée)_ | **Ticket** · Urgente |
| `PD` | Partial delivery | Livraison partielle : des colis manquent. _(sans date estimée)_ | **Ticket** · Urgente |
| `DS` | Destroyed / disposal | Envoi détruit. _(sans date estimée)_ | **Ticket** · Urgente |

### Incidents DHL · 6 codes

| Code | Libellé DHL | Ce que ça veut dire | Alerte |
|---|---|---|---|
| `OH` | On hold | Envoi en attente chez DHL. | Mattermost |
| `SS` | Shipment stopped | Envoi stoppé. _(sans date estimée)_ | **Ticket** · Moyenne |
| `MS` | Mis-sort | Erreur de tri, réacheminement en cours. | **Ticket** · Moyenne |
| `MC` | Miscode | Erreur de code ou d'étiquette. | **Ticket** · Moyenne |
| `TP` | Forwarded to 3rd party - no DD's | Confié à un transporteur tiers, plus de suivi de livraison. _(sans date estimée)_ | — |
| `CS` | Closed shipment | Dossier d'envoi clôturé. _(sans date estimée)_ | — |

Vu sur nos envois mais absent de la liste DHL : `SD`, « Shipment information received ». DHL prévient que de nouveaux codes peuvent apparaître. Un code inconnu n'ouvre jamais de ticket.

## Quand un ticket est ouvert

Il n'y a pas de ticket par envoi. Le tracker alerte seulement quand DHL signale un problème et que les quatre conditions sont réunies. `MD` `NH` `OH` sont signalés sur Mattermost sans ticket.

1. **Dernier événement DHL** : le tracker lit l'événement le plus récent de l'envoi.
2. **Code de problème** : son code fait partie des 19 codes de problème.
3. **Pas encore signalé** : le statut Odoo de l'envoi ne porte pas déjà ce code.
4. **Récent** : l'événement date de moins de 7 jours. Les vieilles histoires sont notées dans Odoo, sans ticket.

### Ticket Helpdesk

- Équipe Logistics & Shipping, tag Shipping Related.
- Client et commande de vente liés, avec les liens vers la livraison et le suivi DHL.
- Un nouveau problème sur un envoi dont le ticket est encore ouvert devient une note interne sur ce ticket, pas un second ticket.
- Le tracker n'écrit que des notes internes. Le client ne reçoit aucun e-mail.

### Message Mattermost

Un message par alerte, avec la couleur de la famille et le lien vers le ticket. Actif dès que l'adresse du canal est configurée (`ALERT_WEBHOOK_URL`).

### Statut dans Odoo

Le champ statut de la livraison garde le dernier code DHL, ce qui évite de signaler deux fois le même problème :

```
Status: [HP] Shipment on hold pending duty payment
Next Steps: Pay the duties online
```

### Priorités et couleurs

| Famille | Priorité du ticket | Couleur Mattermost | Codes |
|---|---|---|---|
| Douane & paiement | Haute | `#F2A30C` | `HP` `CD` `UD` |
| Échec de livraison & adresse | Haute | `#F26B1D` | `ND` `CA` `CC` `BA` `CM` |
| Refus, retour, dommage | Urgente | `#E0341F` | `RD` `RT` `DD` `PD` `DS` |
| Incidents DHL | Moyenne | `#10BCCF` | `SS` `MS` `MC` |
| Sans ticket | Mattermost seulement | — | `MD` `NH` `OH` |

## Ce que l'API renvoie pour nos envois

Vérifié sur de vrais envois Express en transit et livrés.

| Donnée | Champ API | Disponible | Remarque |
|---|---|---|---|
| Statut global | `status.statusCode` | Oui | Toujours présent (champ obligatoire). |
| Code du dernier événement | `events[].status` | Oui | Code Express de 2 lettres. |
| Description | `description` | Oui | En anglais, ou en français avec `language=fr`. |
| Prochaines étapes | `status.nextSteps` | Parfois | Seulement quand DHL a une consigne. |
| Historique complet | `events[]` | Oui | 30 à 45 événements par envoi, avec date, heure et lieu. |
| Lieu de l'événement | `location.address` | Oui | Ville ou hub, par exemple CINCINNATI HUB. |
| Produit | `details.product` | Oui | EXPRESS 12:00 (Y), EXPRESS WORLDWIDE (P). |
| Origine et destination | `origin, destination` | Oui | Ville et pays seulement. |
| Colis | `details.totalNumberOfPieces` | Oui | Nombre de colis et leurs identifiants. |
| Preuve de livraison | `details.proofOfDelivery` | Parfois | Liens vers le POD et la signature, une fois livré. |
| Expéditeur et destinataire | `details.shipper, consignee` | Oui |  |
| Date de livraison estimée | `estimatedTimeOfDelivery` | Non | Vide sur nos envois Express. |
| Poids et dimensions | `details.weight, dimensions` | Non | Vides sur nos envois Express. |

## Limites à connaître

- **Quota DHL** : 250 appels par jour et au plus un appel toutes les 5 secondes, partagés par tous les scripts. Le contrôle horaire coûte 24 appels par jour et par envoi suivi.
- **Fenêtre de 90 jours** : le contrôle automatique ne suit que les livraisons validées dans les 90 derniers jours. Un envoi plus ancien ne se suit plus qu'à la main, par son numéro, avec `shiptracker.py`.
- **Plusieurs numéros dans un seul champ** : Odoo ajoute « ,numéro » à la référence d'un picking chaque fois qu'une étiquette DHL est créée pour lui ou pour un picking lié (étiquette régénérée, retour). Ce sont des envois distincts, pas des colis : un envoi Express de plusieurs colis garde un seul numéro. Le tracker suit chaque numéro, et c'est l'envoi le plus récent qui décide : un retour encore en route garde le picking suivi, une étiquette jamais utilisée ne bloque rien.
- **Suivi expiré** : un picking non livré validé depuis plus de 90 jours passe en « Tracking expired » et n'est plus suivi. Au-delà, DHL ne connaît plus le numéro, ou renvoie les événements d'un autre envoi plus récent : DHL réutilise ses numéros Express.

---

_Les colonnes Alerte, les priorités, les couleurs et les limites sont générées depuis le code du tracker : elles ne peuvent pas diverger de ce qu'il fait._
