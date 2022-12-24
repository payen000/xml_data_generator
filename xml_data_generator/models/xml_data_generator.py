from odoo import _, fields, models
from odoo.exceptions import UserError

UNWANTED_FIELDS = {
    "create_uid",
    "create_date",
    "write_uid",
    "write_date",
    "__last_update",
}


class XmlDataGenerator(models.TransientModel):
    _name = "xml.data.generator"
    _description = "Model to handle exporting record data to XML files."

    model_name = fields.Char()
    res_id = fields.Integer("Record ID")
    mode = fields.Selection(
        [("demo", "Export Anonymized Data"), ("real", "Export Real Data")],
        default="real",
        required=True,
    )

    def _get_records_to_export(self):
        if self.model_name not in self.env:
            raise UserError(_("Please enter a valid model name."))
        records = self.env[self.model_name].browse(self.res_id)
        return records

    def _xml_data_generator_get_field_data(self, record, field_name, field_object, ttype):
        current_value = record[field_name]
        # Anonimyze record char/text data
        if self.mode == "demo" and ttype in ["char", "text"] and current_value:
            current_value = "Demo %s" % field_name
            # If the target model has a method for anonimyzing the field, use it instead
            if hasattr(record, "_xml_data_generator_get_demo_%s" % field_name):
                current_value = getattr(record, "_xml_data_generator_get_demo_%s" % field_name)()
        default_value = field_object.default and field_object.default(record) or False
        # Exclude non-boolean False fields and fields that have the same value as their defaults
        if (not ttype == "boolean" and not current_value) or default_value == current_value:
            return {}
        return {field_name: {"value": current_value, "ttype": ttype}}

    def _prepare_data_to_export(self, records):
        field_objects = records._fields
        field_names = [
            field
            for field in field_objects
            if field_objects[field].compute is None and not field.startswith("image_") and field not in UNWANTED_FIELDS
        ]
        field_records = self.env["ir.model.fields"].search(
            [
                ("model", "=", self.model_name),
                ("name", "in", list(field_names)),
            ]
        )
        data = []
        for record in records:
            field_data = {"xml_id": record.get_external_id()[record.id]}
            for field in field_names:
                ttype = field_records.filtered(lambda f: f.name == field).ttype
                field_values = self._xml_data_generator_get_field_data(record, field, field_objects[field], ttype)
                field_data.update(field_values)
            data.append(field_data)
        return data

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

    def _prepare_xml_row_to_append(self, field_name, dataset):
        field_value = dataset[field_name]["value"]
        field_ttype = dataset[field_name]["ttype"]
        row_dict = {"tab": "    ", "field": field_name}
        if field_ttype not in ["one2many", "many2many", "many2one"]:
            row_dict["field_value"] = field_value
            if field_ttype == "boolean":
                return '%(tab)s%(tab)s<field name="%(field)s" eval="%(field_value)s" />' % row_dict
            return '%(tab)s%(tab)s<field name="%(field)s">%(field_value)s</field>' % row_dict
        table_name = field_value._table
        xml_ids = []
        for id_ in field_value.ids:
            record_xid = field_value.get_external_id()[id_]
            if field_ttype == "many2one":
                row_dict["ref_value"] = self._prepare_xml_id(record_xid, table_name, id_)
                return '%(tab)s%(tab)s<field name="%(field)s" ref="%(ref_value)s" />' % row_dict
            xml_ids.append("ref('%s')" % self._prepare_xml_id(record_xid, table_name, id_))
        eval_value = "[Command.set([%s])]" % ", ".join(xml_ids)
        row_dict["eval_value"] = eval_value
        row = '%(tab)s%(tab)s<field name="%(field)s" eval="%(eval_value)s" />' % row_dict
        # Pre-commit friendly (but hardcoded so it bad)
        if len(row) > 119:
            row_dict["xml_ids"] = ",\n                ".join(xml_ids)
            row_dict["eval_field"] = (
                "[Command.set([" "\n%(tab)s%(tab)s%(tab)s%(tab)s%(xml_ids)s," "\n%(tab)s%(tab)s%(tab)s])]" % row_dict
            )
            # TODO: look for a better way to format these ugly strings
            row = (
                '%(tab)s%(tab)s<field\n%(tab)s%(tab)s%(tab)sname="%(field)s"'
                '\n%(tab)s%(tab)s%(tab)seval="%(eval_value)s"'
                "\n%(tab)s%(tab)s/>" % row_dict
            )
        return row

    def prepare_xml_data_to_export(self, data):
        xml_model = self.model_name.replace(".", "_")
        xml_records_code = []
        for dataset in data:
            xml_code = []
            db_id = dataset.pop("id")["value"]
            xml_id = dataset.pop("xml_id")
            if len(xml_id) == 0 or "_export" in xml_id or "import_" in xml_id:
                if self.mode == "demo":
                    xml_id = "%s_demo_%s" % (xml_model, db_id)
                else:
                    xml_id = "%s_%s" % (xml_model, db_id)
            xml_code.append('    <record id="%s" model="%s">' % (xml_id, self.model_name))
            for field_name in dataset:
                row2append = self._prepare_xml_row_to_append(field_name, dataset)
                xml_code.append(row2append)
            xml_code.append("    </record>")
            xml_records_code.append("\n".join(xml_code))
        return '\n<?xml version="1.0" ?>\n<odoo>\n%s\n</odoo>' % "\n\n".join(xml_records_code)

    def action_import_to_xml(self):
        self.ensure_one()
        records2export = self._get_records_to_export()
        data2export = self._prepare_data_to_export(records2export)
        xml_data_string = self.prepare_xml_data_to_export(data2export)
        raise UserError(xml_data_string)
