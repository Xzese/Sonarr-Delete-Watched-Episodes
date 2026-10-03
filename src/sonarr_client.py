"""Bounded HTTP calls without mutation retries or redirects."""


def create_sonarr_client(url, key):
    import requests
    from pyarr import SonarrAPI

    class CheckedSession(requests.Session):
        def request(self, method, url, **kwargs):
            kwargs["timeout"] = (5, 30)
            kwargs["allow_redirects"] = False
            response = super().request(method, url, **kwargs)
            # pyarr 5.2.0 only rejects selected HTTP error codes. Reject every
            # unexpected status before the SDK can treat a DELETE as successful.
            expected = {200, 204} if method.upper() == "DELETE" else {200}
            if response.status_code not in expected:
                raise requests.HTTPError("Unexpected Sonarr HTTP status.", response=response)
            return response

    client = SonarrAPI(url, key)
    client.session.close()
    client.session = CheckedSession()
    return client
