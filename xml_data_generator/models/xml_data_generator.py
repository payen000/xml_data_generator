from os.path import dirname, join, exists
from os import mkdir

from odoo import _, api, fields, models
from odoo.exceptions import UserError, AccessError

UNWANTED_FIELDS = {
    "create_uid",
    "create_date",
    "write_uid",
    "write_date",
    "__last_update",
    "message_ids",
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
        help="Wether to anonimyze Char/Text fields or not."
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
        # Anonimyze record char/text data
        if self.mode == "demo" and ttype in ["char", "text"] and current_value:
            current_value = "Demo %s" % field_name
            # If the target model has a method for anonimyzing the field, use it instead
            if hasattr(record, "_xml_data_generator_get_demo_%s" % field_name):
                current_value = getattr(record, "_xml_data_generator_get_demo_%s" % field_name)()
        # Exclude non-boolean False fields and fields that have the same value as their defaults
        if (not ttype == "boolean" and not current_value) or default_value == current_value:
            return {}
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

    def _prepare_data_to_export(self, records, data, dependency_tree, model_data, recursive_depth):
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
                for r in related_recordset:
                    child_xml_id = self._prepare_xml_id(r.get_external_id()[r.id], xml_model, r.id)
                    child_model = r._name
                    # Do not add one2many records to dependencies (only many2one and many2many)
                    if ttype != "one2many" and xml_id not in dependency_tree.get(child_xml_id, {}):
                        dependency_tree.setdefault(xml_id, {}).update({child_xml_id: child_model})
                    self._prepare_data_to_export(related_recordset, data, dependency_tree, model_data, recursive_depth + 1)
            data[xml_id] = record_data
            model_data.setdefault(model_name, {}).update({xml_id: dependency_tree.get(xml_id, {})})
        return data, dependency_tree, model_data

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
            db_id = dataset.pop("id")["value"]
            model_name = dataset["model_name"]
            xml_model = dataset.pop("xml_model")
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

    def _create_xml_data(self, xml_data_string):
        # TODO: find a better way to find this path or maybe pass the path through args
        # this is asssuming we have the standard module -> models -> model.py structure
        module_path = dirname(dirname(__file__))
        data_dir = join(module_path, "xml_exported_data")
        if not exists(data_dir):
            mkdir(data_dir)
        data_path = join(data_dir, "model_data.xml")
        with open(data_path, "w") as xml_file:
            xml_file.write(xml_data_string)

    {
        'res.partner': {
            'base.res_partner_address_15': {
                'base.res_partner_12': 'res.partner', 
                'base.state_us_5': 'res.country.state', 
                'base.us': 'res.country'
            }, 
            'base.res_partner_address_28': {
                'base.res_partner_12': 'res.partner', 
                'base.state_us_5': 'res.country.state', 
                'base.us': 'res.country'
            }, 
            'base.res_partner_address_16': {
                'base.res_partner_12': 'res.partner', 
                'base.state_us_5': 'res.country.state', 
                'base.us': 'res.country'
            }, 
            'base.res_partner_12': {
                'base.res_partner_category_11': 'res.partner.category', 
                'base.state_us_5': 'res.country.state', 
                'base.us': 'res.country'
            }}
        , 
        'res.partner.category': {
            'base.res_partner_category_11': {

            }
        }, 
        'res.country.state': {
            'base.state_us_5': {
                'base.us': 'res.country'
            }
        }, 
        'res.country': {
            'base.us': {
                'base.USD': 'res.currency'
            }
        }
    }

    def _probe_model(self, xml_ids, model, model_data, ordered_model_data):
        for xml_id in xml_ids:
            dependencies = xml_ids[xml_id]
            for dependency in dependencies:
                ordered_model_data.setdefault(model, [])
                if dependency not in ordered_model_data[model]:
                    if dependency in xml_ids:
                        ordered_model_data[model] = [dependency] + ordered_model_data[model]
                    else:
                        ordered_model_data[model].append(dependency)
                elif dependency not in xml_ids:
                    counter_model = dependencies[dependency]
                    counter_xml_ids = model_data.get(counter_model)
                    if not counter_xml_ids:
                        return
                    self._probe_model(counter_xml_ids, counter_model, model_data, ordered_model_data)

    def _resolve_dependency_tree(self, model_dependencies):
        model_data_keys = list(model_dependencies)
        model_data_keys.reverse()
        ordered_model_data = {}
        for model in model_data_keys:
            xml_ids = model_dependencies[model]
            self._probe_model(xml_ids, model, model_dependencies, ordered_model_data)
        return ordered_model_data

    def action_export_to_xml(self):
        self.ensure_one()
        records2export = self._get_records_to_export()
        data2export, dependency_tree, model_data = self._prepare_data_to_export(records2export, {}, {}, {}, 0)
        resolved_list = self._resolve_dependency_tree(model_data)
        data2export2 = {}
        for model in resolved_list:
            for xml_id in resolved_list[model]:
                if data2export.get(xml_id):
                    data2export2[xml_id] = data2export[xml_id]
        breakpoint()
        xml_data_string = self.prepare_xml_data_to_export(data2export2, dependency_tree)
        if self.show_data_as_error:
            raise UserError(xml_data_string)
        self._create_xml_data(xml_data_string)

    @api.onchange("recursive_depth")
    def _check_recursive_depth(self):
        if self.recursive_depth > 2:
            return {
                'warning': {
                    'title': _('Maximum recommended recursion level exceeded.'),
                    'message': _(
                        'Exceeding 2 recursion levels is not recommended, as record relations '
                        'can grow rapidly without warning and the export operation could be '
                        'really expensive. Proceed with caution or go back to level 2.'
                    )
                }
            }
