from django.urls import path

from . import views

urlpatterns = [
    path("hello/", views.hello, name="hello"),
    path("items/", views.items, name="items"),
    path("items/<int:pk>/", views.item_detail, name="item-detail"),
    path("login/", views.login, name="login"),
    path("slow/", views.slow, name="slow"),
    path("boom/", views.boom, name="boom"),
]
