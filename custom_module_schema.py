"""Validation helpers for safely evolving administrator-created data modules."""


def normalize_custom_fields(fields):
    """Validate and normalize a custom module field-definition list."""
    if not isinstance(fields, list) or not 1 <= len(fields) <= 20:
        raise ValueError("A custom module must define between 1 and 20 fields.")
    clean=[]
    seen=set()
    for item in fields:
        if not isinstance(item,dict):
            raise ValueError("Each custom field must have a valid definition.")
        name=str(item.get("name","")).strip()
        kind=item.get("type","text")
        required=item.get("required",False)
        options=item.get("options",[])
        if not name or len(name)>60:
            raise ValueError("Field names must contain 1–60 characters.")
        if name.casefold() in seen:
            raise ValueError("Field names must be unique within a module.")
        seen.add(name.casefold())
        if kind not in ("text","number","date","choice","notes"):
            raise ValueError(f"Unsupported field type for {name}.")
        if not isinstance(required,bool):
            raise ValueError(f"Required status for {name} must be true or false.")
        if kind=="choice":
            if not isinstance(options,list) or not 1<=len(options)<=30:
                raise ValueError(f"Choice field {name} needs 1–30 options.")
            options=[str(value).strip() for value in options]
            if any(not value or len(value)>60 for value in options) or len({value.casefold() for value in options})!=len(options):
                raise ValueError(f"Options for {name} must be unique, non-empty values of 60 characters or fewer.")
        else:
            options=[]
        clean.append({"name":name,"type":kind,"required":required,"options":options})
    return clean


def validate_custom_field_evolution(module_name, old_fields, new_fields, has_records):
    """Reject schema edits that would make existing custom records ambiguous."""
    if not has_records:
        return
    proposed={field["name"].casefold():field for field in new_fields}
    old_names={field["name"].casefold() for field in old_fields}
    for previous in old_fields:
        revised=proposed.get(previous["name"].casefold())
        if not revised:
            raise ValueError(
                f"Cannot remove {previous['name']} because {module_name} contains saved records. "
                "Add new fields instead; preserve current fields to keep history readable."
            )
        if (revised["name"]!=previous["name"] or
                revised["type"]!=previous.get("type","text") or
                revised["required"]!=bool(previous.get("required",False))):
            raise ValueError(
                f"Cannot rename or change the type/required setting of {previous['name']} while records exist."
            )
        old_options=previous.get("options",[])
        new_options=revised.get("options",[])
        if revised["type"]=="choice" and not {value.casefold() for value in old_options}.issubset({value.casefold() for value in new_options}):
            raise ValueError(
                f"Keep all existing options for {previous['name']} while records exist; new options may be added."
            )
    for added in new_fields:
        if added["name"].casefold() not in old_names and added["required"]:
            raise ValueError(f"New field {added['name']} must be optional while existing records are present.")
