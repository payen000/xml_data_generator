from os import mkdir
from os.path import dirname, exists, join

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError

UNWANTED_FIELDS = {
    "id",
    "create_uid",
    "create_date",
    "write_uid",
    "write_date",
    "__last_update",
}
# "&" MUST go first to avoid ruining the escape of the other characters
SPECIAL_CHARACTER_MAP = {
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
}
TEXT_TTYPES = {
    "char",
    "text",
    "html",
}


class XmlDataGenerator(models.TransientModel):
    _name = "xml.data.generator"
    _description = "Model to handle exporting record data to XML files."

    model_name = fields.Char(required=True)
    res_id = fields.Integer("Record ID", required=True)
    mode = fields.Selection(
        [("demo", "Export Anonymized Data"), ("real", "Export Real Data")],
        default="real",
        required=True,
        help="Wether to anonimyze Char/Text fields or not.",
    )
    recursive_depth = fields.Integer(
        default=0,
        required=True,
        help="How many levels of recursion will be used to export related records.",
    )
    ignore_access = fields.Boolean(
        default=False,
        help="Determines if a field must be ignored in case of access error.",
    )
    show_data_as_error = fields.Boolean(
        default=True,
        help="Show an error message with the data instead of exporting to a file.",
    )

    def _get_records_to_export(self):
        if self.model_name not in self.env:
            raise UserError(_("Please enter a valid model name."))
        records = self.env[self.model_name].browse(self.res_id)
        return records

    def _xml_data_generator_get_field_data(self, record, field_name, field_object, ttype):
        # Check access, and if access is being ignored, return empty field
        try:
            current_value = record[field_name]
            default_value = field_object.default and field_object.default(record) or False
        except AccessError as e:
            if not self.ignore_access:
                raise AccessError(e)
            return {}
        # Anonimyze record text data
        if self.mode == "demo" and ttype in TEXT_TTYPES and current_value:
            current_value = "Demo %s" % field_name
            # If the target model has a method for anonimyzing the field, use it instead
            if hasattr(record, "_xml_data_generator_get_demo_%s" % field_name):
                current_value = getattr(record, "_xml_data_generator_get_demo_%s" % field_name)()
        # Exclude non-boolean False fields and fields that have the same value as their defaults
        if (not ttype == "boolean" and not current_value) or default_value == current_value:
            return {}
        if ttype == "html" and not isinstance(current_value, str):
            current_value = current_value.unescape()
        if ttype in TEXT_TTYPES and current_value:
            for char in SPECIAL_CHARACTER_MAP:
                current_value = current_value.replace(char, SPECIAL_CHARACTER_MAP[char])
        return {field_name: {"value": current_value, "ttype": ttype}}

    def _prepare_xml_id(self, record_xid, table_name, id_):
        if (
            len(record_xid) > 0
            and "_export" not in record_xid
            and "import_" not in record_xid
            and "base_import" not in record_xid
        ):
            return record_xid
        if self.mode == "demo":
            return "%s_demo_%s" % (table_name, id_)
        return "%s_%s" % (table_name, id_)

    def _prepare_data_to_export(self, records, data, dependency_tree, dependency_data, recursive_depth):
        if recursive_depth > self.recursive_depth:
            return data
        model_name = records._name
        xml_model = model_name.replace(".", "_")
        field_objects = records._fields
        field_names = [
            field
            for field in field_objects
            if field_objects[field].compute is None and not field.startswith("image_") and field not in UNWANTED_FIELDS
        ]
        field_records = self.env["ir.model.fields"].search(
            [
                ("model", "=", model_name),
                ("name", "in", list(field_names)),
            ]
        )
        for record in records:
            xml_id = self._prepare_xml_id(record.get_external_id()[record.id], xml_model, record.id)
            record_data = {"model_name": model_name, "xml_model": xml_model}
            for field in field_names:
                ttype = field_records.filtered(lambda f: f.name == field).ttype
                field_values = self._xml_data_generator_get_field_data(record, field, field_objects[field], ttype)
                record_data.update(field_values)
                if ttype not in ["one2many", "many2one", "many2many"] or not field_values:
                    continue
                related_recordset = field_values[field]["value"]
                for related_record in related_recordset:
                    child_xml_id = self._prepare_xml_id(
                        related_record.get_external_id()[related_record.id], xml_model, related_record.id
                    )
                    child_model = related_record._name
                    # Do not add one2many records to dependencies (only many2one and many2many)
                    if ttype != "one2many" and xml_id not in dependency_tree.get(child_xml_id, {}):
                        dependency_data["model_dependencies"].setdefault(model_name, set()).add(child_model)
                        dependency_tree.setdefault(xml_id, set()).add(child_xml_id)
                    self._prepare_data_to_export(
                        related_recordset, data, dependency_tree, dependency_data, recursive_depth + 1
                    )
            data.setdefault(model_name, {}).update({xml_id: record_data})
            dependency_data["record_dependencies"].update({xml_id: dependency_tree.get(xml_id, {})})
        return data, dependency_data

    def _prepare_xml_row_to_append(self, field_name, dataset, dependency_tree):
        field_value = dataset[field_name]["value"]
        field_ttype = dataset[field_name]["ttype"]
        row_dict = {"tab": "    ", "field": field_name}
        # Do not add one2many rows (they will be handled in the "many" side of the relation)
        if field_ttype == "one2many":
            return None
        if field_ttype not in ["many2many", "many2one"]:
            row_dict["field_value"] = field_value
            if field_ttype == "boolean":
                return '%(tab)s%(tab)s<field name="%(field)s" eval="%(field_value)s" />' % row_dict
            return '%(tab)s%(tab)s<field name="%(field)s">%(field_value)s</field>' % row_dict
        table_name = field_value._table
        xml_ids = []
        for id_ in field_value.ids:
            record_xid = field_value.get_external_id()[id_]
            if record_xid not in dependency_tree:
                continue
            if field_ttype == "many2one":
                row_dict["ref_value"] = self._prepare_xml_id(record_xid, table_name, id_)
                return '%(tab)s%(tab)s<field name="%(field)s" ref="%(ref_value)s" />' % row_dict
            xml_ids.append("ref('%s')" % self._prepare_xml_id(record_xid, table_name, id_))
        if not xml_ids:
            return None
        row_dict["eval_value"] = "[Command.set([%s])]" % ", ".join(xml_ids)
        row = '%(tab)s%(tab)s<field name="%(field)s" eval="%(eval_value)s" />' % row_dict
        # Pre-commit friendly (but hardcoded so it bad)
        if len(row) > 119:
            row_dict["xml_ids"] = ",\n                ".join(xml_ids)
            row_dict["eval_value"] = (
                "[Command.set([\n%(tab)s%(tab)s%(tab)s%(tab)s%(xml_ids)s,\n%(tab)s%(tab)s%(tab)s])]" % row_dict
            )
            # TODO: look for a better way to format these ugly strings
            row = (
                '%(tab)s%(tab)s<field\n%(tab)s%(tab)s%(tab)sname="%(field)s"'
                '\n%(tab)s%(tab)s%(tab)seval="%(eval_value)s"'
                "\n%(tab)s%(tab)s/>" % row_dict
            )
        return row

    def prepare_xml_data_to_export(self, data, dependency_tree):
        xml_records_code = []
        for xml_id, dataset in data.items():
            xml_code = []
            model_name = dataset["model_name"]
            dataset.pop("xml_model")
            xml_code.append('    <record id="%s" model="%s">' % (xml_id, model_name))
            for field_name in dataset:
                if field_name == "model_name":
                    continue
                row2append = self._prepare_xml_row_to_append(field_name, dataset, dependency_tree)
                if row2append:
                    xml_code.append(row2append)
            xml_code.append("    </record>")
            xml_records_code.append("\n".join(xml_code))
        return '<?xml version="1.0" ?>\n<odoo>\n%s\n</odoo>\n' % "\n\n".join(xml_records_code)

    def _create_xml_data(self, file_strings):
        # TODO: find a better way to find this path or maybe pass the path through args
        # this is asssuming we have the standard module -> models -> model.py structure
        module_path = dirname(dirname(__file__))
        data_dir = join(module_path, "xml_exported_data")
        if not exists(data_dir):
            mkdir(data_dir)
        for model in file_strings:
            model_name = model.replace(".", "_")
            xml_data_string = file_strings[model]
            data_path = join(data_dir, "%s.xml" % model_name)
            with open(data_path, "w") as xml_file:
                xml_file.write(xml_data_string)

    def _resolve_dependency_tree(self, data2rearrange):
        xml_id_list = list(data2rearrange.keys())
        xml_id_list.reverse()
        rearranged_data = {}
        for xml_id in xml_id_list:
            rearranged_data[xml_id] = data2rearrange.pop(xml_id)
        return rearranged_data

    def _show_xml_data(self, file_strings):
        data2show = "\n".join([file_strings[model] for model in file_strings])
        raise UserError(data2show)

    def action_export_to_xml(self):
        self.ensure_one()
        records2export = self._get_records_to_export()
        data2export, dependency_data = self._prepare_data_to_export(
            records2export,
            {},
            {},
            {"record_dependencies": {}, "model_dependencies": {}},
            0,
        )
        file_strings = {}
        # TODO: use model_dependencies to suggest a dependency order
        for model in dependency_data["model_dependencies"]:
            model_data = self._resolve_dependency_tree(data2export[model])
            xml_data_string = self.prepare_xml_data_to_export(model_data, dependency_data["record_dependencies"])
            file_strings.update({model: xml_data_string})
        if self.show_data_as_error:
            self._show_xml_data(file_strings)
        self._create_xml_data(file_strings)

    @api.onchange("recursive_depth")
    def _check_recursive_depth(self):
        if self.recursive_depth > 2:
            return {
                "warning": {
                    "title": _("Maximum recommended recursion level exceeded."),
                    "message": _(
                        "Exceeding 2 recursion levels is not recommended, as record relations "
                        "can grow rapidly without warning and the export operation could be "
                        "really expensive. Proceed with caution or go back to level 2."
                    ),
                }
            }
