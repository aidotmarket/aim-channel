# Data-verification commitment-key custody

The data-verification commitment key is a customer-held secret used for stable
HMAC commitments to source locators and structural object identities. It is not
a cloud credential and is never sent to ai.market.

## Generation and storage

`app/routers/data_verification.py::_commitment_key` loads the key from
`<data_directory>/.data_verification_commitment_key`. If that file does not
exist, AIM Data generates 32 bytes with `secrets.token_bytes(32)`, writes the
base64-encoded value, and sets the new file mode to `0600`. An unreadable,
invalid-base64, or non-32-byte existing file causes the verification runtime to
fail closed with a fixed local error.

The current loader sets permissions when it creates the file. It does not
repair or validate the mode of an existing file, so custody of the data
directory and permission monitoring remain local operator responsibilities.

## Rotation and stable comparison

Version 1 intentionally has no rotation operation, key version, or key history.
Using the same key makes commitments for the same registered locator or object
stable across scans. Replacing the key changes those commitments and makes old
and new reports unlinkable by commitment, so a key change cannot be treated as
a transparent rotation.

If the key file is absent, the implemented loader generates a new key. After a
loss or deliberate deletion, earlier commitments cannot be reproduced and
stable comparison therefore fails closed as a non-match. An existing corrupt
or unreadable key does not trigger replacement; verification remains unavailable
until local custody is resolved.

## Backup or no backup

AIM Data does not create, upload, escrow, or restore a backup of this key. The
operator must choose either to retain an exact protected local backup or to run
with no backup and accept permanent loss of comparison continuity if the file
is lost. A restored backup must reproduce the exact 32 key bytes; restoring any
other value is equivalent to replacing the key.

## Destruction and loss

AIM Data exposes no key-destruction command and makes no secure-erasure claim.
Intentional destruction is an operator action covering the key file and every
operator-created backup. On the next verification runtime after the file is
removed, the implemented loader creates a new independent key, and prior
commitments remain unrecoverable from AIM Data or ai.market.

There is no cloud recovery path. ai.market receives only HMAC outputs and has no
commitment key, escrow copy, derivation material, or key selector. Support staff
and receipt verification keys cannot recover the commitment key.

## Separation from the receipt key

The commitment key is separate from the Ed25519 install receipt-signing key.
The receipt private key is obtained from the AIM Data device keystore, while the
commitment key is loaded from the dedicated file above. During scanner
construction, AIM Data rejects a commitment key whose first 32 bytes equal the
raw Ed25519 private key. Only the Ed25519 public verification key and its
`install_key_id` are available to the cloud; neither identifies, derives, or
selects the commitment key.
