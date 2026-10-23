# logs

pingT writes here by default:

| File | Content |
|---|---|
| `pingT-events.log` | every event with date and time, added to on every run |
| `pingT-events-summary-<date>-<time>.txt` | the summary of each run |
| `pingT-events-baseline.json` | which hosts were online, for a restart without `--fresh` |

Everything here except this README is ignored by git. After a migration, move the files off
the box or delete them. Delete the baseline too, unless you still need to restart pingT for that
migration (then keep it, or pingT treats hosts that are down as "never answered").
