# 0016 — Two-factor sign-in: authenticator app, security keys, recovery codes

- **Status:** Done
- **Depends on:** foundation.security (captcha, throttles, `LoginView`)
- **Django apps:** `foundation.mfa` (new), on `allauth.mfa`; `foundation.security`
- **Touches:** mfa / security / settings

## Why
A password alone is one leak away from someone else's account. People should be able to add
their own second factors (an authenticator app on their phone, security keys or passkeys) and
then be asked for one every time they sign in, so a stolen password isn't enough. Every project
on the package should get this the same way, without each building it.

## Scope
- In: a signed-in person setting up, naming and removing their own second factors: one
  authenticator app (TOTP), any number of security keys or passkeys (WebAuthn), and recovery
  codes for when they lose them. Signing in through the API (`LoginView`) asks for one of them
  as a second step once any is set up. So does the Django admin's login (app or recovery code).
  A superuser can clear a person's factors in Django admin when they've lost them all.
- Out (later specs): signing in with a passkey alone (passwordless); "trust this browser"; making
  2FA compulsory (for staff, or everyone); emails when a factor is added or removed; security
  keys on the Django admin's login; the frontend (the package has none; the README describes
  the flow for a SPA).

## Design notes
- **allauth.mfa underneath.** Its `Authenticator` model (one row per factor: `totp`,
  `recovery_codes`, `webauthn`), its TOTP and recovery-code checks, and its WebAuthn wrapper
  around `fido2`. Not its views or flows: those are session- and template-based, and this API is
  JWT. Ours are DRF views in `foundation.mfa`, with the work in `foundation/mfa/services.py`.
  `django-allauth[mfa]` is pinned to `>=65.23,<66` because we use its `internal` modules.
  allauth.mfa allows one authenticator app per person (a database constraint); several devices
  means several keys.
- **Installing:** `allauth`, `allauth.account`, `allauth.mfa`, `foundation.mfa` in
  `INSTALLED_APPS`; `*foundation.MFA_MIDDLEWARE` (allauth's `AccountMiddleware`);
  `globals().update(foundation.mfa(env, issuer="My App"))`;
  `path("api/auth/mfa/", include("foundation.mfa.urls"))`.
- **Secrets encrypted at rest.** TOTP keys and recovery-code seeds go through our allauth
  adapter, which encrypts with Fernet. `MFA_ENCRYPTION_KEY` (from `Fernet.generate_key()`) is
  required; several, comma-separated, rotate keys: the first encrypts, any of them decrypts.
- **Holding setup state without a session.** The TOTP secret being set up, and WebAuthn
  challenges, sit in the Postgres cache for 10 minutes, keyed by the person (setup) or the
  second-step token (sign-in), and are deleted once used.
- **Changes need the password.** Turning on the app, adding a key, removing either and making
  new recovery codes all take `password`, so a stolen access token can't add the thief's
  device or remove the owner's. Renaming a key doesn't. These are throttled per person at the
  `mfa` rate (default `10/min`).
- **Recovery codes** (10, 8 digits) are made with the first factor and shown in that response
  only. Afterwards the overview shows how many are left; new ones replace them all. Removing
  the last factor removes the codes, and 2FA is off.
- **Endpoints** (under `api/auth/mfa/`, signed in unless noted):
  - `GET ""`: `{"enabled", "totp": {...}|null, "keys": [{id, name, created_at, last_used_at}],
    "recovery_codes": {"left", "total"}|null}`.
  - `POST totp/setup/` → `{"secret", "url", "svg"}` (the `otpauth://` link and a QR code).
  - `POST totp/` `{code, password}` → 201 `{"recovery_codes": [...]|null}`.
    `DELETE totp/` `{password}`.
  - `POST keys/options/` → `{"publicKey": ...}` for `navigator.credentials.create`.
  - `POST keys/` `{name, credential, password}` → 201 `{"id", "name", "recovery_codes"}`.
    `PATCH keys/<id>/` `{name}`. `DELETE keys/<id>/` `{password}`.
  - `POST recovery-codes/` `{password}` → `{"recovery_codes": [...]}`.
  - Public, for sign-in: `POST login/key-options/` `{mfa_token}` → `{"publicKey": ...}`;
    `POST login/` `{mfa_token, code}` or `{mfa_token, credential}` → `{"access", "refresh"}`.
- **Sign-in.** `foundation.security.views.LoginView` checks the password (and captcha) as now.
  When `foundation.mfa` is installed and the person has a factor, it answers 200
  `{"mfa_required": true, "mfa_token", "methods": [...]}` instead of tokens. The token is
  signed by Django (not a JWT, so it can't be used as one), lasts `MFA_LOGIN_TIMEOUT` (300 s),
  and works once. `login/` then gives the same tokens the project's
  `SIMPLE_JWT["TOKEN_OBTAIN_SERIALIZER"]` would have. Wrong codes count per person: after
  `MFA_MAX_FAILURES` (5) the second step is refused for `MFA_LOCKOUT_SECONDS` (900), whatever
  the token. `login/` is also under the per-IP `auth` throttle.
- **WebAuthn relying party:** `WEBAUTHN_RP_ID` (the site's domain; default the request's host)
  and the issuer as its name. Browsers only offer a key on the domain it was made for.
- **Django admin login** adds an optional "Authentication code" field: required, and checked
  after the password, for people with 2FA. App or recovery code.
- **Resetting:** allauth.mfa's admin lists `Authenticator` rows; a superuser deletes a
  person's to turn their 2FA off.

## Scenarios

### AT-0016-01 — Without 2FA, nothing is set up and sign-in is as before
- Given I have no second factor
- When I look at my 2FA overview
- Then it says 2FA is off, with no app, no keys and no recovery codes
- And signing in with my password and the captcha gives me tokens straight away

### AT-0016-02 — I set up an authenticator app and get recovery codes once
- When I start setting up an app
- Then I get a secret, an `otpauth://` link and a QR code
- When I confirm with a current code from the app and my password
- Then 2FA is on, and the response shows my 10 recovery codes
- And the overview shows the app and "10 of 10" codes left, but never the codes

### AT-0016-03 — A wrong code or password doesn't turn the app on
- Given I've started setting up an app
- When I confirm with a wrong code, or a right code and a wrong password, or without starting
- Then I'm told why, and 2FA stays off

### AT-0016-04 — I can only have one authenticator app
- Given I already have an app
- When I start setting up another
- Then I'm told to remove the one I have first

### AT-0016-05 — I add security keys, as many as I like, each with a name
- When I add a key named "Blue YubiKey" with my password, answering the challenge I was given
- Then it's listed by that name, 2FA is on and I get recovery codes (if it's my first factor)
- And I can add a second key, "Laptop passkey", and both are listed

### AT-0016-06 — A key that doesn't answer my challenge isn't added
- When the key's answer is to a different challenge, or I never asked for one, or my password is wrong
- Then nothing is added

### AT-0016-07 — I rename a key
- When I rename "Blue YubiKey" to "Keyring key"
- Then that's its name in the list

### AT-0016-08 — I remove a factor with my password; removing the last turns 2FA off
- Given I have an app and a key
- When I remove the key with a wrong password, nothing changes
- When I remove the key, then the app, with my password
- Then 2FA is off and my recovery codes are gone

### AT-0016-09 — New recovery codes replace the old ones
- Given I have 2FA and have used one recovery code
- When I ask for new codes with my password
- Then I get 10 new ones, and the old ones no longer work

### AT-0016-10 — I only see and change my own factors
- Given someone else has a key
- When I rename or remove it by its id
- Then I get 404, and their key is unchanged
- And their app and keys never appear in my overview

### AT-0016-11 — Secrets are stored encrypted, and need the key
- When I set up an app
- Then neither its secret nor the recovery-code seed is in the database in the clear
- And leaving `MFA_ENCRYPTION_KEY` unset stops the project from starting
- And after adding a new key in front of the old one, existing apps still work

### AT-0016-12 — Changes that take a password are limited per person
- When I send more than the `mfa` rate of password-checked requests
- Then I'm told to slow down

### AT-0016-13 — With 2FA, my password alone gives no tokens
- Given I have an app and a key
- When I sign in with my password and the captcha
- Then I get no tokens, only a second-step token and the methods I can use

### AT-0016-14 — A code from my app completes sign-in
- When I send the second-step token and a current code from my app
- Then I get access and refresh tokens that work on the API, and the app's "last used" is set

### AT-0016-15 — A recovery code completes sign-in, once
- When I sign in with one of my recovery codes
- Then I get tokens, and the overview shows one fewer left
- And that code doesn't work again

### AT-0016-16 — A security key completes sign-in
- When I ask for the key challenge with my second-step token and answer it with my key
- Then I get tokens
- And an answer from a key that isn't mine, or to another challenge, gives none

### AT-0016-17 — Wrong codes are limited per person
- When I send a wrong code, I get no tokens
- And after 5 wrong codes, even a right one is refused for 15 minutes, with a new second-step
  token too

### AT-0016-18 — An app code and a second-step token each work once
- Given I signed in with a code
- When the same code, or the same second-step token, is sent again
- Then no tokens are given

### AT-0016-19 — The second-step token expires and can't be forged or used as an access token
- When the token is older than 5 minutes, or altered, or sent as a bearer token
- Then it gets nothing

### AT-0016-20 — The Django admin's login asks for my code
- Given I'm staff with an app
- When I sign in to the admin with my password and the captcha but no code, or a wrong one
- Then I'm not signed in
- And with a current code, or a recovery code, I am; staff without 2FA leave the field empty

### AT-0016-21 — A superuser can turn off someone's 2FA in admin
- Given someone lost their phone and keys
- When a superuser deletes their factors in Django admin
- Then that person signs in with their password alone

## Implementation notes
- `src/foundation/mfa/`: `services.py` (all the work), `views.py`/`urls.py` (DRF), `crypto.py`
  (Fernet, `MultiFernet` for rotation), `adapter.py` (allauth.mfa adapter: encryption, issuer,
  relying party from `MFA_WEBAUTHN_RP_ID` or the request's host), `apps.py` (system checks
  E002/E003). `foundation.settings.mfa()` and `MFA_MIDDLEWARE`; a `mfa` throttle rate.
- `foundation.security`: `LoginView.post` hands off to `services.second_step` for people with
  a factor; `AdminLoginForm` has the `code` field (removed when `foundation.mfa` isn't
  installed) and checks it after the password, with the same lockout.
- Dependency: `django-allauth[mfa]>=65.23,<66` (brings `fido2`, `qrcode`; `cryptography` comes
  with `fido2`). We use `allauth.mfa`'s `internal` TOTP, recovery-code and WebAuthn modules,
  hence the pin.
- Deviations: setup state and WebAuthn challenges live in the cache rather than allauth's
  session. Keys ask for a resident key as "preferred" (allauth's 2FA keys "discouraged") so
  phones can make passkeys. TOTP tolerance is one step either side (allauth's default is 0).
  `LoginView` still lets SimpleJWT's serializer make tokens before it drops them for 2FA
  people, so `last_login` moves at the password step, as before.
- Tests: `tests/test_mfa.py`, one per scenario; `tests/webauthn.py` is a software ES256 key
  that fido2's real server code checks. Not tested: a real browser or hardware key (origins,
  user verification, passkey sync), and the lockout lifting after 15 minutes (it is a cache
  timeout).
