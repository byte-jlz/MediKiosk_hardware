"""Offline check of the QR token shown by the patient's mobile app.

The token is a short-lived JWT signed by the backend with a PRIVATE key. The kiosk holds only the
PUBLIC key, so it can verify tokens without internet and cannot forge them.
Required claims: sub (opaque patient ref), jti (unique id), iat, exp, aud.
"""
import jwt
from edge_db import utc_now, _Tx


class QRRejected(Exception):
    pass


def verify_qr(conn, token, public_key_pem, audience="medikiosk", leeway_s=30):
    try:
        claims = jwt.decode(token, public_key_pem, algorithms=["EdDSA"], audience=audience,
                            options={"require": ["exp", "iat", "jti", "sub"]}, leeway=leeway_s)
    except jwt.PyJWTError as e:
        raise QRRejected(f"invalid QR: {type(e).__name__}") from e
    with _Tx(conn):
        if conn.execute("SELECT 1 FROM qr_seen WHERE jti=?", (claims["jti"],)).fetchone():
            raise QRRejected("QR code already used")
        conn.execute("INSERT INTO qr_seen(jti, seen_at) VALUES(?,?)", (claims["jti"], utc_now()))
    return claims["sub"], claims["jti"]
