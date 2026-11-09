from django import forms


class GroupEmailForm(forms.Form):
    to = forms.ChoiceField(label="To")
    subject = forms.CharField(max_length=150)
    body = forms.CharField(
        label="Message",
        max_length=10000,
        widget=forms.Textarea(attrs={"rows": 12}),
        help_text="Plain text. A line saying who sent it and why they got it is added at the end; replies come to you.",
    )

    def __init__(self, *args, groups, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["to"].choices = [("", "Choose…")] + [
            (group.key, group.label[:1].upper() + group.label[1:]) for group in groups
        ]
