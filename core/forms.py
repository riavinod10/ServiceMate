from django import forms
from django.contrib.auth.forms import UserCreationForm
from django.contrib.auth.models import User


class RegistrationForm(UserCreationForm):
    class Meta:
        model = User
        fields = ("username", "password1", "password2")


class ServiceRequestForm(forms.Form):
    """Free-text service request submitted by the user on the Create Request page.

    Only `request_text` is required. The optional fields let users who know
    their details skip the AI extraction step for those values, but they can
    also leave everything to the Requirement Agent.
    """

    request_text = forms.CharField(
        label="Describe your request",
        widget=forms.Textarea(attrs={
            "rows": 4,
            "placeholder": (
                'e.g. "My AC is not cooling. Find three reliable technicians '
                'near me under ₹1,500, tomorrow evening."'
            ),
            "class": "sm-field__input",
            "autofocus": True,
        }),
        max_length=1000,
        help_text="Describe the problem, service you need, location, budget and preferred time.",
    )

    location = forms.CharField(
        label="Your area / locality",
        required=False,
        max_length=200,
        widget=forms.TextInput(attrs={
            "placeholder": "e.g. Kothrud, Pune",
            "class": "sm-field__input",
        }),
        help_text="Optional — helps ServiceMate find providers near you faster.",
    )

    preferred_date = forms.DateField(
        label="Preferred date",
        required=False,
        widget=forms.DateInput(attrs={
            "type": "date",
            "class": "sm-field__input",
        }),
        help_text="Optional — leave blank to let ServiceMate pick up the date from your description.",
    )

    budget_inr = forms.IntegerField(
        label="Budget (₹)",
        required=False,
        min_value=0,
        max_value=1_000_000,
        widget=forms.NumberInput(attrs={
            "placeholder": "e.g. 1500",
            "class": "sm-field__input",
        }),
        help_text="Optional — maximum you want to spend, in rupees.",
    )


class SettingsForm(forms.Form):
    """User preferences stored in UserProfile (first_name/last_name on the
    built-in User model, plus optional preferred city)."""

    first_name = forms.CharField(
        label="First name",
        required=False,
        max_length=150,
        widget=forms.TextInput(attrs={"class": "sm-field__input"}),
    )
    last_name = forms.CharField(
        label="Last name",
        required=False,
        max_length=150,
        widget=forms.TextInput(attrs={"class": "sm-field__input"}),
    )
    email = forms.EmailField(
        label="Email address",
        required=False,
        widget=forms.EmailInput(attrs={"class": "sm-field__input"}),
    )
    preferred_city = forms.CharField(
        label="Preferred city",
        required=False,
        max_length=100,
        initial="Pune",
        widget=forms.TextInput(attrs={
            "class": "sm-field__input",
            "placeholder": "Pune",
        }),
        help_text="ServiceMate defaults searches to this city.",
    )
