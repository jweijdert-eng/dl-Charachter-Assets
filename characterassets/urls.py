"""App URLs — Character Assets."""

from django.urls import path

from characterassets import views

app_name = "characterassets"

urlpatterns = [
    path("", views.zoeken, name="index"),
    path("zoeken/", views.zoeken, name="zoeken"),
    path("boom/", views.boom, name="boom"),
]
