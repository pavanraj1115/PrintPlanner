frappe.ui.form.on('Print Build', {
    onload(frm) {
        update_all_build_files_percentage_state(frm);
    },

    refresh(frm) {
        setup_queries(frm);
        update_all_build_files_percentage_state(frm);

        if (frm.doc.custom_drive_link) {
            frm.add_custom_button(__('📁 Open Drive Folder'), () => {
                window.open(frm.doc.custom_drive_link, '_blank');
            });
        }

        if (frm.doc.docstatus === 0) {
            frm.add_custom_button(__('Get Items'), () => fetch_unprinted_files(frm));
            frm.add_custom_button(__('Get Project Parts'), () => open_project_parts_picker(frm));
            frm.add_custom_button(__('Suggest Best Spool'), () => suggest_best_spool(frm));

            if (!frm.doc.start_time) {
                frm.add_custom_button('Start', () => {
                    frm.set_value('start_time', frappe.datetime.now_datetime());
                    frm.save().then(() => {
                        if (frm.doc.printer) {
                            frappe.call({
                                method: 'frappe.client.set_value',
                                args: { doctype: 'Workstation', name: frm.doc.printer, fieldname: 'custom_printer_status', value: 'In Production' }
                            });
                        }
                    });
                });
            } else if (!frm.doc.end_time) {
                frm.add_custom_button('Finish', () => {
                    frm.set_value('end_time', frappe.datetime.now_datetime());
                    frm.save().then(() => {
                        if (frm.doc.printer) {
                            frappe.call({
                                method: 'frappe.client.set_value',
                                args: { doctype: 'Workstation', name: frm.doc.printer, fieldname: 'custom_printer_status', value: 'Idle' }
                            });
                        }
                    });
                });
            }

            if (frm.doc.build_files && frm.doc.build_files.length) {
                frm.add_custom_button('Abandon Build (Release Files)', () => {
                    frappe.confirm('This will release every file in this build back to "Not Printed" so they can be picked up elsewhere. Continue?', () => {
                        release_rows(frm.doc.build_files).then(() => {
                            frappe.show_alert({ message: 'Files released', indicator: 'blue' });
                        });
                    });
                });
            }
        }

        if (frm.doc.docstatus === 1 && frm.doc.status === 'Failed') {
            frappe.show_alert({
                message: 'This build failed — use Duplicate (top-right menu) to retry, clearing timing/result fields on the copy before submitting.',
                indicator: 'orange'
            }, 10);
        }
    },

    material(frm) {
        if (frm.doc.spool) {
            frappe.db.get_value('Filament Spool', frm.doc.spool, 'material_item').then(r => {
                if (r && r.message && r.message.material_item !== frm.doc.material) {
                    frm.set_value('spool', '');
                    frm.set_value('spool_available_weight', 0);
                }
            });
        }
        fetch_unprinted_files(frm);
    },

    spool(frm) {
        if (frm.doc.spool) {
            frappe.db.get_value('Filament Spool', frm.doc.spool, ['available_weight', 'status']).then(r => {
                let avail = (r && r.message && flt(r.message.available_weight)) || 0;
                frm.set_value('spool_available_weight', avail);
                let total_weight = (frm.doc.build_files || []).reduce((sum, r) => sum + (flt(r.weight) || 0), 0);
                if (avail < total_weight) {
                    frappe.show_alert({
                        message: __('Warning: Selected spool has only {0}g available, but build needs {1}g!', [avail.toFixed(1), total_weight.toFixed(1)]),
                        indicator: 'orange'
                    }, 5);
                }
            });
        } else {
            frm.set_value('spool_available_weight', 0);
        }
    },

    project(frm) {
        fetch_unprinted_files(frm);
    },

    sales_orders_add(frm) {
        fetch_unprinted_files(frm);
    },

    sales_orders_remove(frm) {
        fetch_unprinted_files(frm);
    },

    before_submit(frm) {
        let missing = (frm.doc.build_files || []).filter(r => !r.result_status);
        if (missing.length) {
            let idx = frm.doc.build_files.indexOf(missing[0]) + 1;
            frappe.throw(`Row #${idx}: Result (success/Failed) must be set for every file before submitting.`);
        }

        (frm.doc.build_files || []).forEach((row, i) => {
            let res = (row.result_status || '').toLowerCase();
            if (res === 'success') {
                row.percentage = 100;
            } else if (res === 'failed' && (row.percentage == null || row.percentage === '')) {
                frappe.throw(`Row #${i + 1}: Please enter the failed completion percentage for ${row.file_name || 'item'}.`);
            }
        });
    },

    before_save(frm) {
        (frm.doc.build_files || []).forEach(row => {
            let res = (row.result_status || '').toLowerCase();
            if (res === 'success') {
                row.percentage = 100;
            }
        });

        if (frm.doc.docstatus === 0 && frm.doc.build_files && frm.doc.build_files.length) {
            return reserve_rows(frm.doc.build_files);
        }
    },

    on_submit(frm) {
        let success_part_weight = 0;
        let success_support_weight = 0;
        let failed_scrap_weight = 0;

        (frm.doc.build_files || []).forEach(row => {
            let p_weight = flt(row.weight) || 0;
            let s_weight = flt(row.support_weight) || 0;
            let tot_weight = p_weight + s_weight;

            if ((row.result_status || '').toLowerCase() === 'success') {
                success_part_weight += p_weight;
                success_support_weight += s_weight;
            } else {
                let pct = (flt(row.percentage) || 0) / 100;
                failed_scrap_weight += tot_weight * pct;
            }
        });

        let total_consumed = success_part_weight + success_support_weight + failed_scrap_weight;
        let total_scrap_weight = success_support_weight + failed_scrap_weight;

        if (frm.doc.spool && total_consumed > 0) {
            deduct_filament_from_spool(frm.doc.spool, total_consumed, frm.doc.name);
        }

        finalize_rows(frm.doc.build_files, frm.doc.name).then(async () => {
            try {
                if (success_part_weight > 0) {
                    await create_and_submit_stock_entries_for_build(frm);
                }

                if (total_scrap_weight > 0) {
                    await create_and_submit_scrap_stock_entry(frm.doc.material, total_scrap_weight, frm.doc.name, success_support_weight, failed_scrap_weight);
                }

                frappe.show_alert({ message: __('Stock Entries created and submitted successfully'), indicator: 'green' });
            } catch (err) {
                console.error('Stock Entry creation failed:', err);
                frappe.msgprint(__('One or more Stock Entries failed to create. Check browser console for details.'));
            }
        });
    },

    on_cancel(frm) {
        release_rows(frm.doc.build_files);
        if (frm.doc.printer) {
            frappe.call({
                method: 'frappe.client.set_value',
                args: { doctype: 'Workstation', name: frm.doc.printer, fieldname: 'custom_printer_status', value: 'Idle' }
            });
        }
    }
});

// Child table: Print Build File event handlers
frappe.ui.form.on('Print Build File', {
    result_status(frm, cdt, cdn) {
        handle_build_file_result(frm, cdt, cdn);
    },
    form_render(frm, cdt, cdn) {
        handle_build_file_result(frm, cdt, cdn);
    },
    percentage(frm, cdt, cdn) {
        let row = locals[cdt][cdn];
        let res = (row.result_status || '').toLowerCase();
        if (res === 'success') {
            if (flt(row.percentage) !== 100) {
                frappe.model.set_value(cdt, cdn, 'percentage', 100);
            }
        }
    }
});

function handle_build_file_result(frm, cdt, cdn) {
    let row = locals[cdt][cdn];
    let res = (row.result_status || '').toLowerCase();
    let is_success = (res === 'success');
    let is_failed = (res === 'failed');

    if (is_success) {
        frappe.model.set_value(cdt, cdn, 'percentage', 100);
    } else if (is_failed) {
        if (flt(row.percentage) === 100) {
            frappe.model.set_value(cdt, cdn, 'percentage', 0);
        }
    }

    let grid = frm.fields_dict && frm.fields_dict['build_files'] && frm.fields_dict['build_files'].grid;
    let grid_row = (grid && grid.grid_rows_by_docname) ? grid.grid_rows_by_docname[cdn] : null;
    if (grid_row && grid_row.toggle_enable) {
        grid_row.toggle_enable('percentage', !is_success);
    }
}

function update_all_build_files_percentage_state(frm) {
    let grid = frm.fields_dict && frm.fields_dict['build_files'] && frm.fields_dict['build_files'].grid;
    (frm.doc.build_files || []).forEach(row => {
        let res = (row.result_status || '').toLowerCase();
        let is_success = (res === 'success');
        if (is_success && flt(row.percentage) !== 100) {
            frappe.model.set_value(row.doctype, row.name, 'percentage', 100);
        }
        let grid_row = (grid && grid.grid_rows_by_docname) ? grid.grid_rows_by_docname[row.name] : null;
        if (grid_row && grid_row.toggle_enable) {
            grid_row.toggle_enable('percentage', !is_success);
        }
    });
}

function setup_queries(frm) {
    if (frm.fields_dict && frm.fields_dict['printer']) {
        frm.set_query('printer', () => {
            return { filters: { custom_printer_status: 'Idle' } };
        });
    }
    if (frm.fields_dict && frm.fields_dict['spool']) {
        frm.set_query('spool', () => {
            return {
                filters: {
                    material_item: frm.doc.material,
                    status: ['!=', 'Depleted']
                }
            };
        });
    }
    if (frm.fields_dict && frm.fields_dict['sales_orders'] && frm.fields_dict['sales_orders'].grid) {
        frm.set_query('sales_order', 'sales_orders', () => {
            let filters = {
                docstatus: ['<', 2],
                status: ['not in', ['Cancelled', 'Completed', 'Closed']]
            };
            if (frm.doc.material) {
                filters['custom_file_details.material_item'] = frm.doc.material;
                filters['custom_file_details.status'] = 'Not Printed';
            }
            return { filters };
        });
    }
}

function create_and_submit_scrap_stock_entry(material, total_scrap, build_name, support_w, failed_w) {
    if (!material || total_scrap <= 0) return Promise.resolve();

    let details = [];
    if (support_w > 0) details.push('Support: ' + support_w.toFixed(1) + 'g');
    if (failed_w > 0) details.push('Failed prints: ' + failed_w.toFixed(1) + 'g');
    let remarks = 'Print Build ' + build_name + ' scrap (' + (details.join(', ') || (total_scrap.toFixed(1) + 'g')) + ')';

    let se_doc = {
        doctype: 'Stock Entry',
        company: frappe.defaults.get_default('company') || 'Protovalue3D',
        purpose: 'Material Transfer',
        stock_entry_type: 'Material Transfer',
        remarks: remarks,
        items: [
            {
                item_code: material,
                s_warehouse: 'Stores - PVlabs',
                t_warehouse: 'Scrap - PVlabs',
                qty: total_scrap
            }
        ]
    };

    return frappe.call({
        method: 'frappe.client.insert',
        args: { doc: se_doc }
    }).then(res => {
        if (res && res.message) {
            return frappe.call({
                method: 'frappe.client.submit',
                args: { doc: res.message }
            });
        }
    }).catch(err => {
        console.error('Failed to create scrap Stock Entry:', err);
    });
}

function deduct_filament_from_spool(spool_name, consumed_g, build_name) {
    if (!spool_name || consumed_g <= 0) return Promise.resolve();
    return frappe.db.get_doc('Filament Spool', spool_name).then(spool => {
        if (!spool) return;
        let old_used = flt(spool.used_weight) || 0;
        let init = flt(spool.initial_weight) || 1000;
        let new_used = old_used + consumed_g;
        let new_avail = Math.max(0, init - new_used);
        let new_status = new_avail <= 20 ? 'Depleted' : (new_avail <= 100 ? 'Low (<100g)' : 'In Use');

        return frappe.call({
            method: 'frappe.client.set_value',
            args: {
                doctype: 'Filament Spool',
                name: spool.name,
                fieldname: {
                    used_weight: new_used,
                    available_weight: new_avail,
                    status: new_status
                }
            }
        }).then(() => {
            frappe.show_alert({
                message: __('Deducted {0}g from Spool {1} (Remaining: {2}g)', [consumed_g.toFixed(1), spool_name, new_avail.toFixed(1)]),
                indicator: 'blue'
            });
        });
    });
}

function suggest_best_spool(frm) {
    if (!frm.doc.material) {
        frappe.msgprint(__('Please select a Material first.'));
        return;
    }
    let total_job_weight = (frm.doc.build_files || []).reduce((sum, r) => sum + (flt(r.weight) || 0), 0);
    if (total_job_weight <= 0) {
        frappe.msgprint(__('No files with weight found in Build Files table. Click "Get Items" first.'));
        return;
    }

    frappe.db.get_list('Filament Spool', {
        filters: {
            material_item: frm.doc.material,
            status: ['!=', 'Depleted']
        },
        fields: ['name', 'spool_id', 'available_weight', 'status', 'color', 'location'],
        limit: 50
    }).then(records => {
        if (!records || records.length === 0) {
            frappe.msgprint(__('No active spools found in stock for material: {0}', [frm.doc.material]));
            return;
        }

        let sufficient = records.filter(s => flt(s.available_weight) >= total_job_weight);

        if (sufficient.length > 0) {
            sufficient.sort((a, b) => flt(a.available_weight) - flt(b.available_weight));
            let best = sufficient[0];
            frm.set_value('spool', best.name).then(() => {
                frm.set_value('spool_available_weight', flt(best.available_weight));
                frappe.show_alert({
                    message: __('Selected Best-Fit Spool: {0} ({1}g available for {2}g build)', [best.spool_id || best.name, flt(best.available_weight).toFixed(1), total_job_weight.toFixed(1)]),
                    indicator: 'green'
                });
            });
        } else {
            records.sort((a, b) => flt(b.available_weight) - flt(a.available_weight));
            let largest = records[0];
            frappe.msgprint({
                title: __('Filament Insufficient'),
                indicator: 'orange',
                message: __('No single spool has at least <b>{0}g</b> available.<br><br>The fullest available spool is <b>{1}</b> with only <b>{2}g</b> remaining.<br>Please prepare a new spool or plan for a mid-print spool change.', [total_job_weight.toFixed(1), largest.spool_id || largest.name, flt(largest.available_weight).toFixed(1)])
            });
        }
    });
}

function fetch_unprinted_files(frm) {
    if (!frm.doc.material) {
        frappe.msgprint(__('Please select a Material first.'));
        frm.clear_table('build_files');
        frm.refresh_field('build_files');
        return;
    }

    let has_so = frm.doc.sales_orders && frm.doc.sales_orders.length;
    let has_proj = !!frm.doc.project;

    frm.clear_table('build_files');
    let promises = [];

    // 1. Fetch unprinted files from specified Sales Orders
    if (has_so) {
        let so_list = frm.doc.sales_orders.map(r => r.sales_order);
        let so_p = frappe.call({
            method: 'frappe.client.get_list',
            args: {
                doctype: 'Sales Order',
                filters: [
                    ['name', 'in', so_list],
                    ['custom_file_details.material_item', '=', frm.doc.material],
                    ['custom_file_details.status', '=', 'Not Printed']
                ],
                fields: [
                    'name as sales_order',
                    'order_type as order_type',
                    'custom_file_details.name as file_row',
                    'custom_file_details.file_name as file_name',
                    'custom_file_details.weight as weight',
                    'custom_file_details.support_weight as support_weight',
                    'custom_file_details.custom_finished_good as custom_finished_good'
                ],
                limit_page_length: 0
            }
        }).then(r => {
            (r.message || []).forEach(f => {
                let child = frm.add_child('build_files');
                let target_wh = 'Finished Goods - PVlabs';
                let fg_good = f.custom_finished_good || '';
                let ot = (f.order_type || '').trim();

                if (ot === 'Display Parts') {
                    target_wh = 'DisplayParts - PVlabs';
                    if (!fg_good) fg_good = get_display_fg_for_material(frm.doc.material);
                } else if (ot === 'Product Development') {
                    target_wh = 'Testing - PVlabs';
                    if (!fg_good) fg_good = get_generic_fg_for_material(frm.doc.material);
                } else {
                    target_wh = 'Finished Goods - PVlabs';
                    if (!fg_good) fg_good = get_generic_fg_for_material(frm.doc.material);
                }

                Object.assign(child, {
                    source_type: 'Sales Order',
                    sales_order: f.sales_order,
                    file_row_reference: f.file_row,
                    file_name: f.file_name,
                    weight: f.weight,
                    support_weight: flt(f.support_weight) || 0,
                    target_warehouse: target_wh,
                    custom_finished_good: fg_good
                });
            });
            frm.refresh_field('build_files');
            update_all_build_files_percentage_state(frm);
        });
        promises.push(so_p);
    } else if (!has_proj) {
        // Auto-discover all open Sales Orders (including Shop Orders) with unprinted files for this material
        let disc_p = frappe.call({
            method: 'frappe.client.get_list',
            args: {
                doctype: 'Sales Order',
                filters: [
                    ['docstatus', '<', 2],
                    ['status', 'not in', ['Cancelled', 'Completed', 'Closed']],
                    ['custom_file_details.material_item', '=', frm.doc.material],
                    ['custom_file_details.status', '=', 'Not Printed']
                ],
                fields: [
                    'name as sales_order',
                    'order_type as order_type',
                    'custom_file_details.name as file_row',
                    'custom_file_details.file_name as file_name',
                    'custom_file_details.weight as weight',
                    'custom_file_details.support_weight as support_weight',
                    'custom_file_details.custom_finished_good as custom_finished_good'
                ],
                limit_page_length: 0
            }
        }).then(r => {
            let files = (r.message || []).filter(f => f.file_row && f.file_name);
            if (!files.length) {
                frappe.msgprint(__('No unprinted files found for material {0} across open Sales Orders.', [frm.doc.material]));
                return;
            }

            let unique_sos = [...new Set(files.map(f => f.sales_order))];
            unique_sos.forEach(so => {
                let so_child = frm.add_child('sales_orders');
                so_child.sales_order = so;
            });
            frm.refresh_field('sales_orders');

            files.forEach(f => {
                let child = frm.add_child('build_files');
                let target_wh = 'Finished Goods - PVlabs';
                let fg_good = f.custom_finished_good || '';
                let ot = (f.order_type || '').trim();

                if (ot === 'Display Parts') {
                    target_wh = 'DisplayParts - PVlabs';
                    if (!fg_good) fg_good = get_display_fg_for_material(frm.doc.material);
                } else if (ot === 'Product Development') {
                    target_wh = 'Testing - PVlabs';
                    if (!fg_good) fg_good = get_generic_fg_for_material(frm.doc.material);
                } else {
                    target_wh = 'Finished Goods - PVlabs';
                    if (!fg_good) fg_good = get_generic_fg_for_material(frm.doc.material);
                }

                Object.assign(child, {
                    source_type: 'Sales Order',
                    sales_order: f.sales_order,
                    file_row_reference: f.file_row,
                    file_name: f.file_name,
                    weight: f.weight,
                    support_weight: flt(f.support_weight) || 0,
                    target_warehouse: target_wh,
                    custom_finished_good: fg_good
                });
            });

            frm.refresh_field('build_files');
            update_all_build_files_percentage_state(frm);

            frappe.show_alert({
                message: __('Loaded {0} unprinted file(s) across {1} Sales Order(s).', [files.length, unique_sos.length]),
                indicator: 'green'
            });
        });
        promises.push(disc_p);
    }

    // 2. Fetch unprinted assembly parts from Product Development Project
    if (has_proj) {
        let proj_p = frappe.call({
            method: 'frappe.client.get',
            args: {
                doctype: 'Product Development Project',
                name: frm.doc.project
            }
        }).then(r => {
            let proj_doc = r.message;
            if (proj_doc && proj_doc.prototype_parts) {
                proj_doc.prototype_parts.forEach(part => {
                    let is_matching_mat = (part.material_item === frm.doc.material);
                    let is_unprinted = (part.status === 'Not Printed' || !part.status);

                    if (is_matching_mat && is_unprinted) {
                        let child = frm.add_child('build_files');
                        let label = part.test_number ? `[Test ${part.test_number}] ${part.file_name}` : part.file_name;
                        Object.assign(child, {
                            source_type: 'Product Development Project',
                            project: frm.doc.project,
                            iteration_ref: part.name,
                            file_row_reference: part.name,
                            file_name: label,
                            weight: flt(part.weight) || 0,
                            target_warehouse: 'Testing - PVlabs',
                            custom_finished_good: get_generic_fg_for_material(frm.doc.material)
                        });
                    }
                });
            }
        });
        promises.push(proj_p);
    }

    Promise.all(promises).then(() => {
        frm.refresh_field('build_files');
        update_all_build_files_percentage_state(frm);
        let count = frm.doc.build_files ? frm.doc.build_files.length : 0;
        frappe.show_alert({
            message: count ? `${count} file(s) added to build` : 'No unprinted files found for selected material',
            indicator: count ? 'green' : 'orange'
        });
    });
}

function reserve_rows(build_rows) {
    let promises = [];

    // Group SO rows
    let so_rows = build_rows.filter(r => r.source_type !== 'Product Development Project' && r.sales_order);
    if (so_rows.length) {
        so_rows.forEach(build_row => {
            if (!build_row.file_row_reference) return;
            let p = frappe.call({
                method: 'frappe.client.set_value',
                args: {
                    doctype: 'sales order files',
                    name: build_row.file_row_reference,
                    fieldname: 'status',
                    value: 'Build'
                }
            });
            promises.push(p);
        });
    }

    // Group Project rows
    let proj_rows = build_rows.filter(r => r.source_type === 'Product Development Project' && r.project);
    if (proj_rows.length) {
        let byProj = {};
        proj_rows.forEach(row => {
            byProj[row.project] = byProj[row.project] || [];
            byProj[row.project].push(row);
        });
        Object.keys(byProj).forEach(proj_name => {
            let p = frappe.call({ method: 'frappe.client.get', args: { doctype: 'Product Development Project', name: proj_name } })
                .then(r => {
                    let proj_doc = r && r.message;
                    if (!proj_doc) return;
                    let changed = false;
                    byProj[proj_name].forEach(build_row => {
                        let part = (proj_doc.prototype_parts || []).find(p => p.name === build_row.file_row_reference);
                        if (part && part.status !== 'Build') {
                            part.status = 'Build';
                            changed = true;
                        }
                    });
                    if (changed) {
                        return frappe.call({ method: 'frappe.client.save', args: { doc: proj_doc } });
                    }
                });
            promises.push(p);
        });
    }

    return Promise.all(promises);
}

function finalize_rows(build_rows, build_name) {
    let promises = [];

    // Finalize SO rows grouped by sales_order
    let so_rows = build_rows.filter(r => r.source_type !== 'Product Development Project' && r.sales_order);
    if (so_rows.length) {
        so_rows.forEach(build_row => {
            if (!build_row.file_row_reference) return;
            let is_success = ((build_row.result_status || '').toLowerCase() === 'success');
            let new_status = is_success ? 'Completed' : 'Not Printed';
            let new_build = is_success ? build_name : '';

            let p1 = frappe.call({
                method: 'frappe.client.set_value',
                args: {
                    doctype: 'sales order files',
                    name: build_row.file_row_reference,
                    fieldname: 'status',
                    value: new_status
                }
            });
            let p2 = frappe.call({
                method: 'frappe.client.set_value',
                args: {
                    doctype: 'sales order files',
                    name: build_row.file_row_reference,
                    fieldname: 'build',
                    value: new_build
                }
            });
            promises.push(p1);
            promises.push(p2);
        });
    }

    // Finalize Project rows grouped by project
    let proj_rows = build_rows.filter(r => r.source_type === 'Product Development Project' && r.project);
    if (proj_rows.length) {
        let byProj = {};
        proj_rows.forEach(row => {
            byProj[row.project] = byProj[row.project] || [];
            byProj[row.project].push(row);
        });
        Object.keys(byProj).forEach(proj_name => {
            let p = frappe.call({ method: 'frappe.client.get', args: { doctype: 'Product Development Project', name: proj_name } })
                .then(r => {
                    let proj_doc = r && r.message;
                    if (!proj_doc) return;
                    byProj[proj_name].forEach(build_row => {
                        let part = (proj_doc.prototype_parts || []).find(p => p.name === build_row.file_row_reference);
                        if (part) {
                            if ((build_row.result_status || '').toLowerCase() === 'success') {
                                part.build = build_name;
                                part.status = 'Completed';
                            } else {
                                part.status = 'Not Printed';
                            }
                        }
                    });
                    return frappe.call({ method: 'frappe.client.save', args: { doc: proj_doc } });
                });
            promises.push(p);
        });
    }

    return Promise.all(promises);
}

function release_rows(build_rows) {
    let promises = [];

    // Release SO rows grouped by sales_order
    let so_rows = build_rows.filter(r => r.source_type !== 'Product Development Project' && r.sales_order);
    // Release SO rows grouped by sales_order
    let so_rows = build_rows.filter(r => r.source_type !== 'Product Development Project' && r.sales_order);
    if (so_rows.length) {
        so_rows.forEach(build_row => {
            if (!build_row.file_row_reference) return;
            let p1 = frappe.call({
                method: 'frappe.client.set_value',
                args: {
                    doctype: 'sales order files',
                    name: build_row.file_row_reference,
                    fieldname: 'status',
                    value: 'Not Printed'
                }
            });
            let p2 = frappe.call({
                method: 'frappe.client.set_value',
                args: {
                    doctype: 'sales order files',
                    name: build_row.file_row_reference,
                    fieldname: 'build',
                    value: ''
                }
            });
            promises.push(p1);
            promises.push(p2);
        });
    }

    // Release Project rows grouped by project
    let proj_rows = build_rows.filter(r => r.source_type === 'Product Development Project' && r.project);
    if (proj_rows.length) {
        let byProj = {};
        proj_rows.forEach(row => {
            byProj[row.project] = byProj[row.project] || [];
            byProj[row.project].push(row);
        });
        Object.keys(byProj).forEach(proj_name => {
            let p = frappe.call({ method: 'frappe.client.get', args: { doctype: 'Product Development Project', name: proj_name } })
                .then(r => {
                    let proj_doc = r && r.message;
                    if (!proj_doc) return;
                    let changed = false;
                    byProj[proj_name].forEach(build_row => {
                        let part = (proj_doc.prototype_parts || []).find(p => p.name === build_row.file_row_reference);
                        if (part) {
                            part.status = 'Not Printed';
                            part.build = '';
                            changed = true;
                        }
                    });
                    if (changed) {
                        return frappe.call({ method: 'frappe.client.save', args: { doc: proj_doc } });
                    }
                });
            promises.push(p);
        });
    }

    return Promise.all(promises);
}



function get_display_fg_for_material(mat) {
    let str = (mat || '').toUpperCase();
    if (str.includes('PETG')) return 'DISPLAY_PETG';
    if (str.includes('ABS')) return 'DISPLAY_ABS';
    if (str.includes('RESIN') || str.includes('SLA')) return 'DISPLAY_RESIN';
    if (str.includes('TPU')) return 'DISPLAY_TPU';
    return 'DISPLAY_PLA';
}

function get_generic_fg_for_material(mat) {
    let str = (mat || '').toUpperCase();
    if (str.includes('PETG')) return 'FG_FDM_PETG';
    if (str.includes('ABS')) return 'FG_FDM_ABS';
    if (str.includes('RESIN') || str.includes('SLA')) return 'FG_SLA_RESIN';
    return 'FG_FDM_PLA';
}

function open_project_parts_picker(frm) {
    if (!frm.doc.material) {
        frappe.msgprint(__('Please select a Material first.'));
        return;
    }

    frappe.call({
        method: 'frappe.client.get_list',
        args: {
            doctype: 'Product Development Project',
            filters: {
                status: ['not in', ['Completed', 'Cancelled', 'On Hold']]
            },
            fields: ['name', 'project_name', 'customer', 'status'],
            limit_page_length: 50
        }
    }).then(r => {
        let projects = r && r.message;
        if (!projects || !projects.length) {
            frappe.msgprint(__('No active Product Development Projects found.'));
            return;
        }

        let options = projects.map(p => ({
            value: p.name,
            label: p.name + ' - ' + (p.project_name || '') + ' (' + (p.customer || '') + ')'
        }));

        let default_proj = frm.doc.project || (options[0] && options[0].value);

        let d = new frappe.ui.Dialog({
            title: __('Get Product Development Project Parts'),
            fields: [
                {
                    label: __('Select Project'),
                    fieldname: 'project',
                    fieldtype: 'Select',
                    options: options.map(o => o.value).join('\n'),
                    default: default_proj,
                    reqd: 1
                }
            ],
            primary_action_label: __('Load Parts'),
            primary_action(values) {
                d.hide();
                load_project_parts_into_build(frm, values.project);
            }
        });
        d.show();
    });
}

function load_project_parts_into_build(frm, project_name) {
    frappe.call({
        method: 'frappe.client.get',
        args: {
            doctype: 'Product Development Project',
            name: project_name
        }
    }).then(r => {
        let proj_doc = r && r.message;
        if (!proj_doc || !proj_doc.prototype_parts || !proj_doc.prototype_parts.length) {
            frappe.msgprint(__('No assembly parts found in project {0}.', [project_name]));
            return;
        }

        let added = 0;
        let existing_refs = new Set((frm.doc.build_files || []).map(row => row.file_row_reference));

        proj_doc.prototype_parts.forEach(part => {
            let is_matching_mat = (part.material_item === frm.doc.material);
            let is_unprinted = (part.status === 'Not Printed' || !part.status);

            if (is_matching_mat && is_unprinted && !existing_refs.has(part.name)) {
                let child = frm.add_child('build_files');
                let label = part.test_number ? ('[Test ' + part.test_number + '] ' + part.file_name) : part.file_name;
                Object.assign(child, {
                    source_type: 'Product Development Project',
                    project: project_name,
                    iteration_ref: part.name,
                    file_row_reference: part.name,
                    file_name: label,
                    weight: flt(part.weight) || 0,
                    support_weight: flt(part.support_weight) || 0,
                    target_warehouse: 'Testing - PVlabs',
                    custom_finished_good: get_generic_fg_for_material(frm.doc.material)
                });
                added++;
            }
        });

        if (added > 0) {
            if (!frm.doc.project) {
                frm.set_value('project', project_name);
            }
            frm.refresh_field('build_files');
            update_all_build_files_percentage_state(frm);
            frappe.show_alert({
                message: __('Loaded ' + added + ' part(s) from Project ' + project_name + ' for Testing warehouse.'),
                indicator: 'green'
            });
        } else {
            frappe.msgprint(__('No unprinted parts matching material {0} found in project {1}.', [frm.doc.material, project_name]));
        }
    });
}

async function create_and_submit_stock_entries_for_build(frm) {
    let successful_rows = (frm.doc.build_files || []).filter(r => (r.result_status || '').toLowerCase() === 'success');
    if (!successful_rows.length) return;

    let group_keys = {};
    for (let r of successful_rows) {
        // 1. Determine target warehouse
        let wh = r.target_warehouse;
        if (!wh) {
            if (r.source_type === 'Product Development Project' || r.project) {
                wh = 'Testing - PVlabs';
            } else {
                wh = 'Finished Goods - PVlabs';
            }
        }

        // 2. Determine finished good item code
        let fg_code = r.custom_finished_good;
        if (!fg_code) {
            if (wh === 'DisplayParts - PVlabs') {
                fg_code = get_display_fg_for_material(frm.doc.material);
            } else {
                fg_code = get_generic_fg_for_material(frm.doc.material);
            }
        }

        let key = fg_code + ':::' + wh;
        group_keys[key] = group_keys[key] || { fg_code: fg_code, warehouse: wh, count: 0, total_weight: 0 };
        group_keys[key].count += 1;
        group_keys[key].total_weight += (flt(r.weight) || 0);
    }

    // ERPNext Manufacture requires 1 finished item per Stock Entry.
    // We create one Stock Entry per distinct (finished_good + target_warehouse) group.
    for (let key of Object.keys(group_keys)) {
        let group = group_keys[key];
        if (group.total_weight <= 0) continue;

        let item_res = await frappe.db.get_value('Item', group.fg_code, ['stock_uom', 'is_stock_item']);
        let uom = (item_res && item_res.message && item_res.message.stock_uom) || 'Nos';
        let fg_qty = (uom === 'Gram' || uom === 'g') ? group.total_weight : group.count;

        let se_doc = {
            doctype: 'Stock Entry',
            company: frappe.defaults.get_default('company') || 'Protovalue3D',
            purpose: 'Manufacture',
            stock_entry_type: 'Manufacture',
            remarks: 'Manufactured from Print Build ' + frm.doc.name + ' for ' + group.warehouse,
            items: [
                {
                    item_code: frm.doc.material,
                    s_warehouse: 'Stores - PVlabs',
                    qty: group.total_weight
                },
                {
                    item_code: group.fg_code,
                    t_warehouse: group.warehouse,
                    qty: fg_qty,
                    is_finished_item: 1
                }
            ]
        };

        try {
            let insert_res = await frappe.call({
                method: 'frappe.client.insert',
                args: { doc: se_doc }
            });

            if (insert_res && insert_res.message) {
                await frappe.call({
                    method: 'frappe.client.submit',
                    args: { doc: insert_res.message }
                });
            }
        } catch (e) {
            console.error('Failed to create/submit Stock Entry for group:', group, e);
        }
    }
}