"""Settings and Company Profile views (Person 3)."""
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render

from .forms import SettingsForm
from .models import Provider


@login_required
def settings_view(request):
    """User settings: name, email, preferred city."""
    user = request.user

    initial = {
        "first_name": user.first_name,
        "last_name": user.last_name,
        "email": user.email,
        # Preferred city is stored in session for now; a UserProfile model
        # can be added later without changing this view.
        "preferred_city": request.session.get("preferred_city", "Pune"),
    }

    form = SettingsForm(request.POST or None, initial=initial)

    if request.method == "POST" and form.is_valid():
        user.first_name = form.cleaned_data["first_name"]
        user.last_name = form.cleaned_data["last_name"]
        user.email = form.cleaned_data["email"]
        user.save(update_fields=["first_name", "last_name", "email"])

        city = form.cleaned_data.get("preferred_city", "Pune").strip() or "Pune"
        request.session["preferred_city"] = city

        messages.success(request, "Settings saved.")
        return redirect("settings")

    return render(request, "core/settings.html", {"form": form})


@login_required
def company_profile(request, place_id):
    """Full provider/company profile page."""
    provider = get_object_or_404(Provider, place_id=place_id)

    # Fetch the user's service requests that used this provider, so we can
    # show history relevant to the logged-in user.
    past_bookings = (
        provider.booking_set
        .filter(request__user=request.user)
        .select_related("request")
        .order_by("-created_at")[:5]
    )

    return render(request, "core/company_profile.html", {
        "provider": provider,
        "past_bookings": past_bookings,
    })
