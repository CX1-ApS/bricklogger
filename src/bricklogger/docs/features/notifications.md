# Notifications

Bricklogger can send mail to an administrator: an **alarm** when something
goes wrong, an **all clear** when it is put right, and a **summary** once a
day. The summary is sent whether or not anything is wrong, so a morning
without one says the machine is gone — which is what a monitoring probe on
[`/health`](daemon.md#health) cannot tell you when the whole host is down.

Notifications are **off until they are switched on**, and they can only be
switched on when a model is active. A daemon with nothing to collect has
nothing to report.

## What is sent

Three kinds of mail, all drawn from the same runtime state the
[status tree](daemon.md#status) shows. None of them asks a plugin anything,
on the same principle as status itself.

| Mail | Sent when | Content |
|------|-----------|---------|
| **Alarm** | A warning of kind `operation` is raised for the first time, health becomes `degraded`, or the daemon starts or stops | The conditions that opened, grouped by code, and the health before and after |
| **All clear** | Such a warning is withdrawn, or health leaves `degraded` | The conditions that closed, and what still stands |
| **Summary** | Every day at `digest`, whatever the state | Health, uptime, the active model, the point counts, every instance with its state, every warning that stands, of both kinds, and the newer releases the daily [update check](cli.md#update) found |

An alarm and an all clear are **the two ends of one condition**. That is
possible because every warning in Bricklogger is a condition that holds now,
with a defined end the daemon watches for, as the
[warning list](daemon.md#status) describes. A mail therefore never says
"something happened"; it says "this is true now", or "this is no longer
true".

**The summary is the heartbeat.** It is sent on a green morning too, because
that is what makes its absence mean something. Silence from a system that
writes only when it is unhappy cannot be told apart from a dead machine.

### The daemon's own start and stop

A start and a stop count as `operation` and travel in the same window as
everything else, so an upgrade that stops and starts the daemon gives **one**
mail that says both, not two. An unplanned restart is thereby visible,
without a restart loop being able to fill the mailbox.

A stop is **held rather than sent**, and reported by the start that follows;
that is what keeps the pair to one mail. A daemon stopped and never started
again therefore says nothing on its way down, and is reported instead by the
summary that fails to arrive the next morning — which is what the summary is
for.

**At most 20 events are held at once**, and the twenty-first drops the
oldest. Every event that is held gets a line of its own in the mail, with
what happened and when: unlike the conditions, events are never grouped or
counted, because there are only two kinds of them and they never become
many. Twenty is reached only by a daemon that restarts ten times between
two mails, and a restart loop is a larger problem than the line that fell
off the end. Events are recorded **only while notifications are on**, and
the list is emptied by the mail that carries it.

## Which warnings raise an alarm

Every warning code has a **kind**, given in the
[warning table](daemon.md#status):

- **`operation`** — the logger is not doing its job right now: an instance is
  down, a device answers reads with errors, a spool is dropping observations.
  These raise an alarm as soon as they appear.
- **`model`** — the model or the rule set leaves something unresolved: a point
  without a reference, a rule that could not be evaluated, a unit the graph
  and the protocol disagree about. These appear **only in the summary**.

The split follows how the two behave. A `model` warning is raised when the
plan is evaluated, stands unchanged until the model or the rules change, and
is work for a working day. An `operation` warning means data is not arriving
now. Waking someone at three in the morning over a point whose reference was
never filled in would teach them to ignore the mail that matters.

A deliberately stopped instance (`instance_stopped`) is `operation`, although
it is not a failure and leaves [health](daemon.md#health) at `ok`. An instance
that is not collecting is worth a record either way, and the all clear when it
is started again closes the loop.

Two codes are `operation` but **never** raise a mail, for the plain reason
that they are about the mail itself: `notify_failed`, when a mail could not be
sent, and `notifications_dormant`, when notifications are switched on but no
model is active. Both stand in status and in the log, where the CLI, the web
interface and a health probe find them.

## The window and the floor

A site has thousands of points, and one dead switch raises `read_error` on
every point behind it within seconds. Two settings keep that from becoming
thousands of mails:

- **`window`** — after the first event the daemon waits this long and gathers
  everything else that happens, then sends one mail. Default **`2m`**.
- **`min_interval`** — the floor between two mails. What arrives under the
  floor is held and folded into the next one. Default **`15m`**.

Within a mail the conditions are **grouped by code**, not listed by subject:
one line saying that 142 points on `bacnet_main` stopped answering, with the
first few named and the rest counted. The whole list is always one
`bricklogger points --warning read_error` away, and a mail that has to carry
it is a mail nobody reads.

Both settings hold for alarms and all clears alike, so a device that goes up
and down cannot produce a pair of mails per cycle. The summary is subject to
neither: it is sent at its hour regardless.

## The subject line

The subject names **the building and the machine**:

```
[Baltorpvej 20 / cx1h-tst001] Bricklogger: 2 instances failed
```

The building comes from the active model, which is why a model is a
precondition: an administrator who runs Bricklogger in several buildings has
to tell from the subject alone which one is writing. A model with several
buildings names them all, separated by commas; a model that names none leaves
the machine's host name standing alone. Neither is configured — both are read
where they already are.

## Switching it on

Notifications live in a `notifications` section of
[`daemon.yaml`](configuration.md#daemonyaml), beside `log` and `web`, with
`enabled: false` as the default. The schema is defined in the
[configuration document](configuration.md#notifications).

**Switching them on requires an active model.** `enabled: true` in a config
directory whose data directory holds no active model is a **validation
error** naming `notifications.enabled`, and the write is refused as a whole,
exactly as `api.token` is required on a binding that is not loopback. The
order on a new machine is therefore: upload a model, then switch notifications
on.

**If the model later goes missing** — a data directory deleted while
`enabled: true` stands — the daemon still **starts**. Notifications go
dormant and `notifications_dormant` stands in status until a model is active
again. Refusing to start would take the logger down over a mail setting, which
is the wrong trade, and the daemon cannot mail about the very thing that has
silenced it.

## Delivery

Mail is sent over SMTP from a thread of the daemon's own, on the pattern of
the other background work, so a slow or unreachable mail server never touches
collection.

- **Transport.** `starttls` on the submission port is the default, with `tls`
  for an implicit-TLS port and `none` for a relay on the local network.
  Credentials are optional: many site relays take unauthenticated mail from
  their own hosts. The password is a secret like any other and belongs in the
  [`env` file](configuration.md#the-env-file).
- **Both forms.** Every mail carries a plain-text part and an HTML part. A
  client that renders HTML shows the warnings as a table with the health state
  in the signal colour; everything else reads the text.
- **Retry.** A send that fails is retried with backoff a few times and then
  dropped. What was in it is not lost: an alarm's conditions still stand in
  status, and the next summary carries them.
- **A failure never mails.** `notify_failed` is raised when mail cannot be
  sent, and withdrawn when a mail goes through again.

**What the daemon remembers.** The conditions the administrator has been told
about are kept in the [runtime state](../architecture.md#runtime-state), and so
is the health last reported. An alarm and an all clear are the difference
between that set and what stands now, which is why a restart does not mail
everything that stands all over again. Deleting the runtime state does: the
daemon comes up knowing nothing, and the first window after start carries what
it finds.

**A manual clear is silent.** Clearing a warning by hand is an acknowledgement
rather than an end, so the notifier forgets it along with the warning and sends
no all clear for something that may still be true. A condition that is asserted
again afterwards opens again, and that is worth a mail.

## Status and the test mail

Two commands, defined in the [CLI document](cli.md#notify) and answered by the
endpoints in the [API reference](api.md#notifications):

- **`bricklogger notify test`** sends a test mail at once and prints what the
  server answered, the failure included. It is the command to run on site,
  behind the customer's firewall, before believing that mail leaves the
  machine at all.
- **`bricklogger notify status`** shows whether notifications are on, when the
  last mail went and to whom, the last error, when the next summary is due,
  and what is waiting in the open window right now.

Both have their place on the web interface's
[Notifications screen](web.md#screens), as parity requires.
