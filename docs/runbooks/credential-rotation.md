# Rotating credentials

Two very different procedures. The first is routine; the second has a window in which the
service cannot read any credential at all.

## A provider credential

Models → the model → replace the credential → **Test connection** → save. Effective on the
next request, because a write bumps the gateway config cache version.

The old value is not recoverable and does not need to be — SPEC §5.4 makes credentials
write-only, with no reveal endpoint, so nobody has a copy to be tempted by. The audit log
records the change as `credential: "***" → "***"`, which is the whole point: the fact that
it changed is auditable, the value is not.

Revoke the old key at the provider afterwards, not before.

## `ENCRYPTION_MASTER_KEY`

Every provider credential is encrypted with its own AES-GCM data key, and that data key is
wrapped with the master key. Rotation therefore re-wraps 48 bytes per row and never
decrypts a payload — which is why this takes seconds rather than a re-encryption pass.

**There is a window.** Between the new key being deployed and the re-wrap finishing, the
service cannot decrypt any credential: every upstream call fails with a decryption error.
Do this in a maintenance window, and have the old key to hand.

1. Generate one and keep the current value:

   ```bash
   openssl rand -base64 32
   ```

2. Put the new value in the Secret, keeping the old one somewhere you can read it.

   ```bash
   kubectl -n <ns> create secret generic memory-gateway \
     --from-literal=ENCRYPTION_MASTER_KEY='<new>' --dry-run=client -o yaml \
     | kubectl -n <ns> apply -f -
   kubectl -n <ns> rollout restart deploy/<release>-memory-gateway-api
   ```

3. Re-wrap, immediately:

   ```bash
   kubectl -n <ns> run mg-rotate --rm -it --restart=Never \
     --image=<the same image> \
     --env-from=secret/memory-gateway \
     -- python -m app.cli rotate-master-key --previous '<old base64 key>'
   ```

   `--dry-run` first if you want the count before the change.

4. Verify with **Test connection** on a model that has a credential.

The command is **resumable**: every row is tried against the current key first, so one that
is already re-wrapped is skipped and an interrupted run can simply be run again. A row
readable under neither key is named rather than skipped silently — that means a third key,
or corruption, and somebody has to decide what to do with it.

Keep the old key until step 4 passes. After that, destroy it.

## `JWT_SIGNING_KEY`

Changing it invalidates every access and refresh token: everybody is signed out and signs
in again. There is no rotation procedure because none is needed — that *is* the procedure,
and it is the correct response to a suspected token compromise.
