# Somebody is locked out of the control plane

**Symptom.** A user gets 429s on login with a `Retry-After`. Usually an administrator,
usually during an incident, because that is when people mistype passwords.

**What it means.** Login is throttled per IP *and* per email, in Redis so the limit holds
across replicas. Five failures in fifteen minutes trips it. Both counters exist because
they stop different attacks: per-IP slows one source spraying many accounts, per-email slows
a distributed attack on one account.

A successful login clears the per-email counter but deliberately **not** the per-IP one —
otherwise an attacker with one valid account could reset their own budget between guesses
at everyone else's.

## Fix

Wait it out, or clear it:

```bash
kubectl -n <ns> run mg-unlock --rm -it --restart=Never \
  --image=<the same image> --env-from=secret/memory-gateway \
  -- python -m app.cli unlock-login --email person@example.com
```

`--ip` clears an address, and both can be given at once. It prints how many counters were
actually set, so an unlock that reports zero means the lockout is somewhere else — check
that the organization is not suspended, which refuses login for a different reason and with
a different message.

This is a command on the deployment host rather than an endpoint, on purpose: an unlock
endpoint is a way to reset the counter that an attacker also has.

## If Redis is down

The throttle fails open and logs a warning. Nobody is locked out; nobody is protected
either. See [redis-outage.md](redis-outage.md).

## If it keeps happening

A single account tripping repeatedly is usually an integration retrying with a stale
password rather than a person. The per-email counter's key is a hash, so the logs will not
name it — but the audit log records successful logins, and the gap is informative.
