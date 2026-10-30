"""Edge Read Aloud endpoint compatibility for edge-tts 7.2.8.

Protocol facts verified against Readest commit6ad1e2c (2026-10-11).
Original adapter; no Readest implementation copied. Uses the SDK's existing
public Edge client token/signing/parser, not an Azure account/subscription key.
Remove this adapter when the pinned SDK supports this endpoint natively.
"""
from urllib.parse import urlsplit,parse_qs,urlencode

EDGE_ENDPOINT='wss://api.msedgeservices.com/tts/cognitiveservices/websocket/v1'
LEGACY_ENDPOINT='wss://speech.platform.bing.com/consumer/speech/synthesize/readaloud/edge/v1'


def compatible_url(current,client_token):
    parsed=urlsplit(current)
    base=parsed._replace(query='',fragment='').geturl()
    if base==EDGE_ENDPOINT and parse_qs(parsed.query).get('Ocp-Apim-Subscription-Key')==[client_token]:
        return current
    if base!=LEGACY_ENDPOINT:
        raise RuntimeError('Unexpected Edge speech endpoint. Review the SDK compatibility adapter.')
    return EDGE_ENDPOINT+'?'+urlencode({'Ocp-Apim-Subscription-Key':client_token})


def configure_edge_endpoint():
    # This constant is the only SDK override. Its connection ID, clock-skew
    # correction, signing, TLS validation, metadata and audio parser stay intact.
    import edge_tts.communicate as transport
    from edge_tts.constants import TRUSTED_CLIENT_TOKEN
    transport.WSS_URL=compatible_url(transport.WSS_URL,TRUSTED_CLIENT_TOKEN)
