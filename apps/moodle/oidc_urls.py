"""The "Log in with BBMS" endpoints, mounted at /o/ in the "oauth2_provider" namespace (the toolkit looks its own
endpoints up there)."""

from django.urls import path, re_path
from oauth2_provider import views as provider

from . import oidc

app_name = "oauth2_provider"

# Only the endpoints Moodle needs, not the toolkit's whole set: its application-management pages would let any
# logged-in member register an OAuth application, and its device-flow pages aren't wanted.
urlpatterns = [
    path("authorize/", oidc.BBMSAuthorizationView.as_view(), name="authorize"),
    path("token/", provider.TokenView.as_view(), name="token"),
    path("revoke_token/", provider.RevokeTokenView.as_view(), name="revoke-token"),
    re_path(
        r"^\.well-known/openid-configuration/?$",
        provider.ConnectDiscoveryInfoView.as_view(),
        name="oidc-connect-discovery-info",
    ),
    path(".well-known/jwks.json", provider.JwksInfoView.as_view(), name="jwks-info"),
    path("userinfo/", provider.UserInfoView.as_view(), name="user-info"),
]
