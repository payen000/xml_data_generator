from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError
from odoo.loglevels import ustr
from odoo.tools import topological_sort

UNWANTED_FIELDS = {
    "id",
    "create_uid",
    "create_date",
    "write_uid",
    "write_date",
    "__last_update",
}
# Do not copy documents
UNWANTED_TTYPES = {"binary"}
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

    model_name = fields.Char()
    res_id = fields.Integer("Record ID")
    search_by_external_id = fields.Boolean()
    xml_data_generator_external_id = fields.Char(
        string="Record External ID",
        help="Real external ID - if any, or proposed external ID, if one does not exist.",
    )
    mode = fields.Selection(
        [("demo", "Export Anonymized Data"), ("real", "Export Real Data")],
        default="real",
        required=True,
        help="Wether to anonymize Char/Text fields or not.",
    )
    recursive_depth = fields.Selection(
        [
            ("0", "This record"),
            ("1", "This record and first related records"),
            ("2", "This record, first related records and second related records"),
            ("3", "This record, first related records, second related records and third related records"),
        ],
        string="Records to Export",
        default="0",
        required=True,
    )
    ignore_access = fields.Boolean(
        string="Ignore Restricted Fields",
        default=False,
        help="Determines if a field must be ignored in case of access error.",
    )
    avoid_duplicates = fields.Boolean(
        default=True,
        help="Wether to avoid exporting data that already has an external ID by xml or not.",
    )
    fetched_data = fields.Text(readonly=True)

    @api.model
    def default_get(self, searched_fields):
        res = super().default_get(searched_fields)
        model = res.get("model_name", self._context.get("active_model", ""))
        res_id = res.get("res_id", self._context.get("active_id"))
        ir_model_data = (
            self.env["ir.model.data"]
            .sudo()
            .search_read(
                domain=[("model", "=", model), ("res_id", "=", res_id)],
                fields=["module", "name"],
            )
        )
        if not ir_model_data:
            suffix = "demo" if self.mode == "demo" else "auto"
            res["xml_data_generator_external_id"] = "__xml_data_generator_virtual__.%s_%s_%s" % (
                model.replace(".", "_"),
                suffix,
                res_id,
            )
            return res
        module = ir_model_data[0]["module"]
        name = ir_model_data[0]["name"]
        res["xml_data_generator_external_id"] = "%s.%s" % (module, name)
        return res

    def _prepare_record_to_export(self):
        model_name = self.model_name
        res_id = self.res_id
        if self.search_by_external_id:
            external_id = self.xml_data_generator_external_id
            if not external_id.startswith("__xml_data_generator_virtual__."):
                return self.env.ref(external_id)
            suffix = "_demo_" if self.mode == "demo" else "_auto_"
            record_name = external_id.split("__xml_data_generator_virtual__.", 1)[1]
            record_name_parts = record_name.split(suffix, 1)
            model_name = record_name_parts[0].replace("_", ".")
            res_id = int(record_name_parts[-1])
        return self.env[model_name].browse(res_id)

    def _get_record_to_export(self):
        try:
            return self._prepare_record_to_export()
        except Exception:
            raise UserError(
                _(
                    "Record not found: please make sure the entered External ID exists in the database or is a valid "
                    "proposed xml_data_generator External ID."
                )
            )

    def _xml_data_generator_get_field_data(self, record, field_name, field_object, ttype):
        """ Get the field values for a given record.

        :param record: a record of any given comodel.
        :type record: recordset
        :param field_name: the name of the field to be fetched
        :type field_name: string
        :param field_object: the python field object (not the one in the database)
        :type field_object: odoo.fields

        returns: dict - a dictionary that is empty if the field's value is equal to its default value,
        otherwise it contains:

        value: the field's value, be it a demo value or a real value, can be any primitive or class
        ttype: the field's ttype - string
        related_model: the field's comodel name, if any - string
        """
        # Check access to fields, and if access is being ignored, return empty field
        try:
            current_value = record[field_name]
            default_value = field_object.default and field_object.default(record) or False
        except AccessError as e:
            if not self.ignore_access:
                raise AccessError(e)
            return {}
        # Convert html Markup data to string
        if ttype == "html":
            current_value = ustr(current_value) if current_value else False
            default_value = ustr(default_value) if default_value else False
        # Anonymize record text data, this is preferred when only trying to replicate relations between models
        # also, this option will be forced when the user does not belong to the export group, just in case
        if (
            (self.mode == "demo" or not self.env.user.has_group("base.group_allow_export"))
            and ttype in TEXT_TTYPES
            and current_value
        ):
            current_value = "Demo %s" % field_name
            # If the target model has a method for anonymizing the field, use it instead
            if hasattr(record, "_xml_data_generator_get_demo_%s" % field_name):
                current_value = getattr(record, "_xml_data_generator_get_demo_%s" % field_name)()
        # Exclude non-boolean False fields and fields that have the same value as their defaults
        if (not ttype == "boolean" and not current_value) or default_value == current_value:
            return {}
        if ttype in TEXT_TTYPES and current_value:
            for char in SPECIAL_CHARACTER_MAP:
                current_value = current_value.replace(char, SPECIAL_CHARACTER_MAP[char])
        return {
            field_name: {
                "value": current_value,
                "ttype": ttype,
                "related_model": field_object.comodel_name,
            }
        }

    def _prepare_external_id(self, record_xid, model_name, id_, recursive_depth, is_child_record=False):
        """ Returns either the real or a processed demo External ID."""
        if (
            len(record_xid) > 0
            and "_export" not in record_xid
            and "import_" not in record_xid
            and "base_import" not in record_xid
        ):
            if self.avoid_duplicates and recursive_depth > 0 and not is_child_record:
                return None
            return record_xid
        if self.mode == "demo":
            return "__xml_data_generator_virtual__.%s_demo_%s" % (model_name, id_)
        return "__xml_data_generator_virtual__.%s_auto_%s" % (model_name, id_)

    def _prepare_data_to_export(self, records, data, dependency_tree, dependency_data, recursive_depth):
        """ Recursive method to traverse a recordset's fields and record dependencies.

        :param records: a recordset
        :type records: recorset
        :param data: the same data this method returns
        :type data: dict
        :param dependency_tree: current recordset's dependencies
        :type dependency_tree: dict
        :param dependency_data: whole tree's dependencies
        :type dependency_data: dict
        :param recursive_depth: how many levels of depth related records will be traversed
        :type recursive_depth: integer

        returns:

        data: a dict containing all field values for the whole tree
        dependency_data: a dict of dicts containing which records and models depend on each other
        """
        if recursive_depth > int(self.recursive_depth):
            return data, dependency_data
        model_name = records._name
        xml_model = model_name.replace(".", "_")
        field_objects = records._fields
        # TODO: see if some comodels should be omitted, such as mail.message
        field_map = {
            field: field_objects[field]
            for field in field_objects
            if field_objects[field].compute is None
            and field_objects[field].type not in UNWANTED_TTYPES
            and field not in UNWANTED_FIELDS
        }
        for record in records:
            external_id = self._prepare_external_id(
                record.get_external_id()[record.id], xml_model, record.id, recursive_depth
            )
            if not external_id:
                continue
            record_data = {"model_name": model_name, "xml_model": xml_model}
            for field in field_map:
                ttype = field_map[field].type
                field_values = self._xml_data_generator_get_field_data(record, field, field_map[field], ttype)
                if ttype != "one2many":
                    record_data.update(field_values)
                if ttype not in ["one2many", "many2one", "many2many"] or not field_values:
                    continue
                related_recordset = field_values[field].pop("value")
                child_external_ids = []
                for related_record in related_recordset:
                    child_model = related_record._name
                    child_external_id = self._prepare_external_id(
                        related_record.get_external_id()[related_record.id],
                        child_model.replace(".", "_"),
                        related_record.id,
                        recursive_depth,
                        is_child_record=True,
                    )
                    child_external_ids.append(child_external_id)
                    # Do not add one2many records to dependencies (only many2one and many2many)
                    if ttype != "one2many":
                        # If parent is not in its child's dependencies, add child to dependencies
                        # this check is used to avoid circular dependencies.
                        # Same goes for model dependencies.
                        if external_id not in dependency_tree.get(child_external_id, set()):
                            dependency_tree.setdefault(external_id, set()).add(child_external_id)
                        if model_name not in dependency_data["model_dependencies"].get(child_model, set()):
                            dependency_data["model_dependencies"].setdefault(child_model, set()).add(model_name)
                    self._prepare_data_to_export(
                        related_recordset,
                        data,
                        dependency_tree,
                        dependency_data,
                        recursive_depth + 1,
                    )
                # Replace the records themselves by their external_ids
                field_values[field]["value"] = child_external_ids
            data.setdefault(model_name, {}).update({external_id: record_data})
            dependency_data["record_dependencies"].update({external_id: dependency_tree.get(external_id, {})})
        return data, dependency_data

    def _prepare_xml_row_to_append(self, field_name, field_value, field_ttype, field_related_model):
        row_dict = {"tab": "    ", "field": field_name}
        if field_ttype not in ["many2many", "many2one"]:
            row_dict["field_value"] = field_value
            if field_ttype == "boolean":
                return '%(tab)s%(tab)s<field name="%(field)s" eval="%(field_value)s" />' % row_dict
            return '%(tab)s%(tab)s<field name="%(field)s">%(field_value)s</field>' % row_dict
        field_related_model.replace(".", "_")
        external_ids = []
        for record_xid in field_value:
            if field_ttype == "many2one":
                row_dict["ref_value"] = record_xid
                return '%(tab)s%(tab)s<field name="%(field)s" ref="%(ref_value)s" />' % row_dict
            external_ids.append("ref('%s')" % record_xid)
        if not external_ids:
            return None
        row_dict["eval_value"] = "[Command.set([%s])]" % ", ".join(external_ids)
        row = '%(tab)s%(tab)s<field name="%(field)s" eval="%(eval_value)s" />' % row_dict
        # Pre-commit friendly (but hardcoded so it bad)
        if len(row) > 119:
            row_dict["external_ids"] = ",\n                ".join(external_ids)
            row_dict["eval_value"] = (
                "[Command.set([\n%(tab)s%(tab)s%(tab)s%(tab)s%(external_ids)s,\n%(tab)s%(tab)s%(tab)s])]" % row_dict
            )
            # TODO: look for a better way to format these ugly strings
            row = (
                '%(tab)s%(tab)s<field\n%(tab)s%(tab)s%(tab)sname="%(field)s"'
                '\n%(tab)s%(tab)s%(tab)seval="%(eval_value)s"'
                "\n%(tab)s%(tab)s/>" % row_dict
            )
        return row

    def prepare_xml_data_to_export(self, unsorted_data, sorted_xml_dependencies, sorted_model_dependencies_dict):
        xml_records_code = []
        # This is to fix the order within a single file
        # TODO: improve this logic (perhaps sort the whole data outside in another method)
        data = {
            external_id: unsorted_data[external_id]
            for external_id in sorted_xml_dependencies
            if external_id in unsorted_data
        }
        for external_id, dataset in data.items():
            xml_code = []
            model_name = dataset.pop("model_name")
            dataset.pop("xml_model")
            xml_code.append('    <record id="%s" model="%s">' % (external_id, model_name))
            for field_name in dataset:
                field_value = dataset[field_name]["value"]
                field_ttype = dataset[field_name]["ttype"]
                field_related_model = dataset[field_name]["related_model"]
                # Do not add one2many rows (they will be handled in the "many" side of the relation)
                # also do not add fields for models with "downstream" dependencies, only upwards
                # to avoid defining a field relationship twice (which would result in a dependency error)
                if (
                    field_ttype == "one2many"
                    or field_related_model
                    and sorted_model_dependencies_dict.get(field_related_model, -1)
                    < sorted_model_dependencies_dict.get(model_name, -1)
                ):
                    continue
                row2append = self._prepare_xml_row_to_append(field_name, field_value, field_ttype, field_related_model)
                if row2append:
                    xml_code.append(row2append)
            xml_code.append("    </record>")
            xml_records_code.append("\n".join(xml_code))
        return '<?xml version="1.0" ?>\n<odoo>\n%s\n</odoo>\n' % "\n\n".join(xml_records_code)

    def _get_rebuilt_action(self, file_strings):
        files_list = [file_strings[model] for model in file_strings]
        files_list.reverse()
        data2show = "\n".join(files_list)
        self.fetched_data = data2show
        return {
            "name": "Export to XML",
            "type": "ir.actions.act_window",
            "res_model": "xml.data.generator",
            "view_mode": "form",
            "res_id": self.id,
            "target": "new",
            "context": {
                "default_model_name": self.model_name,
                "default_res_id": self.res_id,
            },
        }

    def action_export_to_xml(self):
        self.ensure_one()
        records2export = self._get_record_to_export()
        if not records2export:
            raise UserError(_("No records found to export."))
        data2export, dependency_data = self._prepare_data_to_export(
            records2export,
            {},
            {},
            {"record_dependencies": {}, "model_dependencies": {}},
            0,
        )
        sorted_xml_dependencies = topological_sort(dependency_data["record_dependencies"])
        sorted_model_dependencies = topological_sort(dependency_data["model_dependencies"])
        # Topological sort leaves out elements without dependencies, so we must add them to the beginning of the list
        # since the list of files is inverted at the end
        sorted_model_dependencies = (
            list(set(data2export.keys()) - set(sorted_model_dependencies)) + sorted_model_dependencies
        )
        sorted_model_dependencies_dict = {model: i for i, model in enumerate(sorted_model_dependencies)}

        file_strings = {}
        for model in sorted_model_dependencies:
            if model not in data2export:
                continue
            xml_data_string = self.prepare_xml_data_to_export(
                data2export[model],
                sorted_xml_dependencies,
                sorted_model_dependencies_dict,
            )
            file_strings.update({model: xml_data_string})
        return self._get_rebuilt_action(file_strings)

    @api.onchange("recursive_depth")
    def _check_recursive_depth(self):
        if int(self.recursive_depth) > 2:
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
