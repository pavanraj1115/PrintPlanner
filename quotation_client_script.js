// Helper to resolve Finished Good code from raw material
function resolve_fg_for_row(cdt, cdn) {
    let row = locals[cdt][cdn];
    let mat_str = ((row.material_item || row.material_general || '')).toUpperCase();
    if (!row.material_item && !row.material_general) {
        frappe.model.set_value(cdt, cdn, 'custom_finished_good', '');
        return Promise.resolve('');
    }
    if (row.material_item) {
        return frappe.db.get_value('Item', row.material_item, ['custom_finished_good_item', 'item_group', 'item_name']).then(r => {
            let d = (r && r.message) || {};
            let fg = d.custom_finished_good_item;
            if (!fg) {
                let str = (row.material_item + ' ' + (row.material_general || '') + ' ' + (d.item_group || '') + ' ' + (d.item_name || '')).toUpperCase();
                if (str.includes('PETG')) fg = 'FG_FDM_PETG';
                else if (str.includes('ABS') || str.includes('ASA')) fg = 'FG_FDM_ABS';
                else if (str.includes('RESIN') || str.includes('SLA')) fg = 'FG_SLA_RESIN';
                else if (str.includes('TPU')) fg = 'FG_FDM_TPU';
                else fg = 'FG_FDM_PLA';
            }
            return frappe.model.set_value(cdt, cdn, 'custom_finished_good', fg);
        });
    } else {
        let fg = 'FG_FDM_PLA';
        if (mat_str.includes('PETG')) fg = 'FG_FDM_PETG';
        else if (mat_str.includes('ABS') || mat_str.includes('ASA')) fg = 'FG_FDM_ABS';
        else if (mat_str.includes('RESIN') || mat_str.includes('SLA')) fg = 'FG_SLA_RESIN';
        else if (mat_str.includes('TPU')) fg = 'FG_FDM_TPU';
        return frappe.model.set_value(cdt, cdn, 'custom_finished_good', fg);
    }
}

frappe.ui.form.on('Quotation', {
    onload(frm) {
        sync_order_type_from_opportunity(frm);
        toggle_quotation_file_details(frm);
        sync_deal_name_from_opportunity(frm);
        sync_drive_link_from_opportunity(frm);
        sync_files_from_opportunity(frm);
        // set_sales_order_route_options(frm);
        if (frm.is_new() && (!frm.doc.items || frm.doc.items.length === 0)) {
            sync_quotation_prices(frm);
        }
    },
    refresh(frm) {
        sync_order_type_from_opportunity(frm);
        toggle_quotation_file_details(frm);
        sync_deal_name_from_opportunity(frm);
        sync_drive_link_from_opportunity(frm);
        sync_files_from_opportunity(frm);
        // set_sales_order_route_options(frm);
        if (frm.is_new() && (!frm.doc.items || frm.doc.items.length === 0)) {
            sync_quotation_prices(frm);
        }

                setup_auto_email_sender();
                hook_email_doc_template(frm);
        frm.add_custom_button(__('Flush / Send Queued Email'), () => {
            flush_email_queue_for_doc(frm);
        }, __('Actions'));

        if (frm.fields_dict && frm.fields_dict['custom_file_details'] && frm.fields_dict['custom_file_details'].grid) {
            frm.fields_dict['custom_file_details'].grid.toggle_display('description', true);

            // Table synchronization action button on the child table grid
            frm.fields_dict['custom_file_details'].grid.add_custom_button(__('🔄 Sync Table to Items'), () => {
                sync_quotation_items_table(frm);
                frappe.show_alert({ message: __('Synchronized file details to items table!'), indicator: 'green' });
            });

            // Action button to fetch files directly from Google Drive
            frm.fields_dict['custom_file_details'].grid.add_custom_button(__('📥 Fetch Files from Drive'), () => {
                fetch_drive_files_to_quotation_table(frm);
            });
        }

        
        if (!frm.doc.custom_google_drive_link) {
            frm.add_custom_button(__('Google Drive Folder'), () => {
                if (frm.is_new()) {
                    frm.save().then(() => {
                        create_and_open_drive_folder(frm);
                    });
                } else {
                    create_and_open_drive_folder(frm);
                }
            }, __('Create'));
        } else {
            frm.add_custom_button(__('Open Drive Folder'), () => {
                window.open(frm.doc.custom_google_drive_link, '_blank');
            }, __('Actions'));
        }

        frm.add_custom_button(__('Attach Versioned PDF'), async () => {
            let deal = frm.doc.custom_deal_name || frm.doc.name;
            let print_format = frm.meta.default_print_format || 'ProtoValue3D Quotation';
            let url = `/api/method/frappe.utils.print_format.download_pdf?doctype=${frm.doctype}&name=${frm.docname}&format=${print_format}&no_letterhead=0`;

            let max_v = 0;
            try {
                let existing = await frappe.db.get_list('File', {
                    filters: {
                        attached_to_doctype: frm.doctype,
                        attached_to_name: frm.docname
                    },
                    fields: ['file_name']
                });
                (existing || []).forEach(f => {
                    let m = (f.file_name || '').match(/_quote_v(\d+)\.pdf/i);
                    if (m && m[1]) {
                        let v = parseInt(m[1], 10);
                        if (!isNaN(v) && v > max_v) max_v = v;
                    }
                });
            } catch (e) {
                console.warn('Could not query existing files:', e);
            }

            let next_v = max_v + 1;
            let target_filename = deal + '_quote_v' + next_v + '.pdf';
            frappe.show_alert({ message: __('Generating ' + target_filename + '...'), indicator: 'blue' });

            fetch(url, { headers: { 'X-Frappe-CSRF-Token': frappe.csrf_token } })
                .then(res => res.blob())
                .then(blob => {
                    let formData = new FormData();
                    formData.append('file', blob, target_filename);
                    formData.append('doctype', frm.doctype);
                    formData.append('docname', frm.docname);
                    formData.append('is_private', 1);

                    return fetch('/api/method/upload_file', {
                        method: 'POST',
                        headers: { 'X-Frappe-CSRF-Token': frappe.csrf_token },
                        body: formData
                    });
                })
                .then(res => res.json())
                .then(r => {
                    if (r.message) {
                        frappe.show_alert({ message: __('Attached: ' + target_filename), indicator: 'green' });
                        frm.reload_doc();
                        window.open(url, '_blank');
                    } else {
                        throw new Error('Upload failed');
                    }
                })
                .catch(err => {
                    console.error(err);
                    frappe.msgprint(__('Failed to attach ' + target_filename + ' — check console.'));
                });
        }, __('Print'));
    },
    order_type(frm) {
        toggle_quotation_file_details(frm);
        sync_quotation_items_table(frm);
    },
    opportunity(frm) {
        sync_order_type_from_opportunity(frm);
        sync_deal_name_from_opportunity(frm);
        sync_drive_link_from_opportunity(frm);
        sync_files_from_opportunity(frm);
        sync_quotation_items_table(frm);
    },
    custom_file_details_remove(frm) {
        sync_quotation_items_table(frm);
    },
    validate(frm) {
        // Only initialize items table if it is completely empty and files exist.
        // Never wipe out or re-create child table rows during validate on an already populated form,
        // as modifying child rows during validation marks the form dirty in Frappe and blocks document submission.
        if ((!frm.doc.items || frm.doc.items.length === 0) && (frm.doc.custom_file_details || []).length > 0) {
            sync_quotation_items_table(frm);
        }
        (frm.doc.items || []).forEach(row => {
            if (!row.uom) row.uom = 'Nos';
            if (!row.stock_uom) row.stock_uom = row.uom || 'Nos';
            if (!row.conversion_factor || flt(row.conversion_factor) === 0) row.conversion_factor = 1.0;
            if (!row.stock_qty) row.stock_qty = (flt(row.qty) || 1) * (flt(row.conversion_factor) || 1);
        });
    }
});

// Row triggers for Print file detail on Quotation
frappe.ui.form.on('Print file detail', {
    cost_per_gram(frm, cdt, cdn) {
        sync_quotation_items_table(frm);
    },
    weight(frm, cdt, cdn) {
        sync_quotation_items_table(frm);
    },
    support_weight(frm, cdt, cdn) {
        sync_quotation_items_table(frm);
    },
    quantity(frm, cdt, cdn) {
        sync_quotation_items_table(frm);
    },
    file_name(frm, cdt, cdn) {
        sync_quotation_items_table(frm);
    },
    material_general(frm, cdt, cdn) {
        resolve_fg_for_row(cdt, cdn).then(() => {
            sync_quotation_items_table(frm);
        });
    },
    material_item(frm, cdt, cdn) {
        let row = locals[cdt][cdn];
        resolve_fg_for_row(cdt, cdn).then(() => {
            let ot = frm.doc.order_type;
            if (ot === 'Miniature Design' || ot === 'Customized 3D') {
                if (flt(row.cost_per_gram) === 0) {
                    frappe.model.set_value(cdt, cdn, 'cost_per_gram', 0);
                }
            }
            sync_quotation_items_table(frm);
        });
    }
});

function sync_quotation_items_table(frm) {
    if (frm.doctype !== 'Quotation') return;
    if (frm.doc.docstatus !== 0) return; // Only sync in Draft mode
    if (frm._syncing_items) return; // Prevent re-entrancy
    frm._syncing_items = true;

    try {
        let ot = frm.doc.order_type;
        let deal = (frm.doc.custom_deal_name || '').toUpperCase();
        let is_min = (ot === 'Miniature Design' || deal.includes('_MIN_'));
        let is_cd = (ot === 'Customized 3D' || deal.includes('_CD_'));
        let is_ps = (ot === '3D Print Service' || deal.includes('_PS_'));
        let is_file_wf = is_min || is_cd || is_ps || ((frm.doc.custom_file_details || []).length > 0);

        // If not a 3D file/printing workflow, return early
        if (!is_file_wf) {
            return;
        }

    let is_zero_material = (is_min || is_cd);
    let conversion_rate = flt(frm.doc.conversion_rate) || 1;

    let file_rows = (frm.doc.custom_file_details || []).filter(r => (r.file_name || r.material_item || (flt(r.weight) + flt(r.support_weight) > 0)));
    let file_material_codes = new Set(file_rows.map(r => r.material_item).filter(Boolean));

    // 1. Collect non-material / design service rows to preserve
    let preserved_items = [];
    (frm.doc.items || []).forEach(i => {
        let code = (i.item_code || '').toLowerCase();
        let is_service = code.includes('miniature') || code.includes('sculpt') || code.includes('design') 
            || code.includes('cad') || code.includes('custom') || code.includes('gift') || code.includes('keychain') 
            || code.includes('toy') || code.includes('lamp') || code.includes('trophy') || code.includes('organizer') 
            || code.includes('award') || code.includes('topper') || code.includes('nameplate') || code.includes('bobblehead') 
            || code.includes('dragon') || code.includes('collectible') || code.includes('tower') || code.includes('bookend') 
            || code.includes('stand') || code.includes('enclosure') || code.includes('reverse');
        let is_fg = (i.item_code || '').startsWith('FG_');

        if (is_service || (!file_material_codes.has(i.item_code) && !is_fg)) {
            let qty = flt(i.qty) || 1;
            let rate = flt(i.rate) || 0;
            let amount = flt(i.amount) || (qty * rate);
            preserved_items.push({
                item_code: i.item_code,
                item_name: i.item_name || i.item_code,
                description: i.description || i.item_code,
                uom: i.uom || 'Nos',
                stock_uom: i.stock_uom || i.uom || 'Nos',
                qty: qty,
                rate: rate,
                price_list_rate: flt(i.price_list_rate) || rate,
                discount_percentage: flt(i.discount_percentage) || 0,
                amount: amount,
                base_rate: flt(i.base_rate) || (rate * conversion_rate),
                base_amount: flt(i.base_amount) || (amount * conversion_rate),
                conversion_factor: flt(i.conversion_factor) || 1
            });
        }
    });

    // If Miniature Design: preserve or add default miniature design service line if empty
    if (is_min) {
        let has_min = preserved_items.some(i => (i.item_code || '').toLowerCase().includes('miniature'));
        if (!has_min) {
            preserved_items.unshift({
                item_code: '4inch miniature design',
                item_name: '4inch miniature design',
                description: '4 Inch Custom Miniature 3D Design & Sculpting Service',
                uom: 'Nos',
                stock_uom: 'Nos',
                qty: 1,
                rate: 0,
                price_list_rate: 0,
                amount: 0,
                base_rate: 0,
                base_amount: 0,
                conversion_factor: 1
            });
        }
    }

    // If Customized 3D: preserve or add default customized line if empty
    if (is_cd) {
        let has_cd = preserved_items.some(i => {
            let c = (i.item_code || '').toLowerCase();
            return c.includes('custom') || c.includes('gift') || c.includes('design');
        });
        if (!has_cd) {
            preserved_items.unshift({
                item_code: 'Customised 3D Models & Gifts',
                item_name: 'Customised 3D Models & Gifts',
                description: 'Bespoke Custom 3D Modeling, Prototyping, and Personalized Gift Design Service',
                uom: 'Nos',
                stock_uom: 'Nos',
                qty: 1,
                rate: 0,
                price_list_rate: 0,
                amount: 0,
                base_rate: 0,
                base_amount: 0,
                conversion_factor: 1
            });
        }
    }

    // 2. Prepare material items from file rows
    // For each unit of quantity in the files table, unpack an individual finished good item row
    let material_items = [];
    file_rows.forEach(row => {
        let weight = flt(row.weight) || 0;
        let support_weight = flt(row.support_weight) || 0;
        let billed_weight = weight + support_weight;
        let units = Math.max(1, parseInt(row.quantity) || 1);
        let cost = flt(row.cost_per_gram) || 0;

        // In Miniature Design & Customized 3D, material printing rate is 0 (covered in design fee)
        // In 3D Print Service, rate is cost_per_gram (default 6.0 if 0)
        let rate = is_zero_material ? 0 : (cost > 0 ? cost : 6.0);
        row.total = billed_weight * (is_zero_material ? cost : rate) * units;

        // Resolve FG code
        let fg_code = row.custom_finished_good;
        if (!fg_code) {
            let u = ((row.material_item || row.material_general) || '').toUpperCase();
            if (u.includes('PETG')) fg_code = 'FG_FDM_PETG';
            else if (u.includes('ABS') || u.includes('ASA')) fg_code = 'FG_FDM_ABS';
            else if (u.includes('RESIN') || u.includes('SLA')) fg_code = 'FG_SLA_RESIN';
            else if (u.includes('TPU')) fg_code = 'FG_FDM_TPU';
            else fg_code = 'FG_FDM_PLA';
            row.custom_finished_good = fg_code;
        }

        let row_desc = support_weight > 0
            ? `${row.file_name || 'Print Model'} (${row.material_general || ''} - ${row.material_item || fg_code}) [Part: ${weight}g, Support: ${support_weight}g]`
            : `${row.file_name || 'Print Model'} (${row.material_general || ''} - ${row.material_item || fg_code}) [${weight}g]`;

        let unit_amount = billed_weight * rate;

        for (let u = 0; u < units; u++) {
            material_items.push({
                item_code: fg_code,
                item_name: fg_code,
                description: row_desc,
                uom: 'Gram',
                stock_uom: 'Gram',
                qty: billed_weight,
                rate: rate,
                price_list_rate: rate,
                discount_percentage: 0,
                amount: unit_amount,
                base_rate: rate * conversion_rate,
                base_amount: unit_amount * conversion_rate,
                conversion_factor: 1
            });
        }
    });

    // 3. Clear and repopulate items table synchronously
    frm.clear_table('items');

    preserved_items.forEach(d => {
        let child = frm.add_child('items');
        Object.assign(child, d);
    });

    material_items.forEach(d => {
        let child = frm.add_child('items');
        Object.assign(child, d);
    });

    frm.refresh_field('items');
    frm.refresh_field('custom_file_details');

    if (frm.cscript && frm.cscript.calculate_taxes_and_totals) {
        frm.cscript.calculate_taxes_and_totals();
    }
    } finally {
        frm._syncing_items = false;
    }
}

function fetch_drive_files_to_quotation_table(frm) {
    if (!frm.doc.custom_google_drive_link) {
        frappe.confirm(
            __('No Google Drive folder linked to this Quotation yet. Would you like to create one now?'),
            () => { create_and_open_drive_folder(frm); }
        );
        return;
    }

    frappe.show_alert({
        message: __('Fetching files from Google Drive...'),
        indicator: 'blue'
    });

    fetch('https://www.protovalue3d.in/api/gdrive/folder', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
            action: 'list',
            folder_url: frm.doc.custom_google_drive_link
        })
    })
    .then(r => r.json())
    .then(data => {
        if (!data.success) {
            frappe.msgprint({
                title: __('Google Drive Sync Error'),
                indicator: 'red',
                message: data.message || 'Could not list files in the Drive folder.'
            });
            return;
        }

        let files = (data.files || []).filter(f => {
            let name = (f.name || '').trim();
            if (!name || name.startsWith('.') || f.mimeType === 'application/vnd.google-apps.folder') {
                return false;
            }
            let lower = name.toLowerCase();
            return lower.endsWith('.stl') || lower.endsWith('.3mf') || lower.endsWith('.step') || lower.endsWith('.stp') || lower.endsWith('.obj');
        });

        if (files.length === 0) {
            frappe.msgprint({
                title: __('No 3D Models in Drive Folder'),
                indicator: 'orange',
                message: __(
                    'No CAD/3D files (.stl, .3mf, .step, .obj) found in the Google Drive folder yet.<br><br>' +
                    'Please upload your sliced models into the folder first.<br><br>' +
                    '<a href="' + frm.doc.custom_google_drive_link + '" target="_blank" class="btn btn-primary btn-sm">' +
                    'Open Google Drive Folder</a>'
                )
            });
            return;
        }

        let existing_names = new Set((frm.doc.custom_file_details || []).map(r => (r.file_name || '').trim().toLowerCase()));
        let added_count = 0;

        files.forEach(f => {
            let fname = f.name.trim();
            if (existing_names.has(fname.toLowerCase())) {
                return;
            }

            let row = frm.add_child('custom_file_details');
            row.file_name = fname;
            row.quantity = 1;
            row.weight = 0;
            row.support_weight = 0;
            row.cost_per_gram = (frm.doc.order_type === 'Miniature Design' || frm.doc.order_type === 'Customized 3D') ? 0 : 6.0;
            row.material_general = 'PLA';
            row.material_item = 'JMG_PLA_B';
            row.custom_finished_good = 'FG_FDM_PLA';
            existing_names.add(fname.toLowerCase());
            added_count++;
        });

        frm.refresh_field('custom_file_details');
        sync_quotation_items_table(frm);

        if (added_count > 0) {
            frappe.show_alert({
                message: __('Added ' + added_count + ' 3D model(s) from Drive! Please enter sliced weights & support weights.'),
                indicator: 'green'
            });
        } else {
            frappe.show_alert({
                message: __('All Drive models are already present in the table.'),
                indicator: 'blue'
            });
        }
    })
    .catch(err => {
        frappe.msgprint({
            title: __('Drive Connection Error'),
            indicator: 'red',
            message: err.message
        });
    });
}

function sync_quotation_prices(frm) {
    if (!frm.doc || frm.doc.docstatus !== 0) return;
    let ot = frm.doc.order_type;
    let is_file_wf = ['3D Print Service', 'Miniature Design', 'Customized 3D'].includes(ot);
    if (is_file_wf) {
        sync_quotation_items_table(frm);
    }
}

function sync_order_type_from_opportunity(frm) {
    if (frm.doc.docstatus !== 0) return;

    let route_order_type = null;
    if (frappe.route_options && frappe.route_options.order_type) {
        route_order_type = frappe.route_options.order_type;
        delete frappe.route_options.order_type;
    }

    let deal_name = frm.doc.custom_deal_name || (frappe.route_options && frappe.route_options.custom_deal_name) || '';
    let is_ds = deal_name.includes('_DS_');
    let is_ps = deal_name.includes('_PS_');
    let is_min = deal_name.includes('_Min_');
    let is_cd = deal_name.includes('_CD_');

    function apply_type(target_type) {
        if (frm.doc.order_type !== target_type) {
            frm.set_value('order_type', target_type);
        }
        if (target_type === 'Sales') {
            if (frm.doc.custom_file_details && frm.doc.custom_file_details.length > 0) {
                frm.clear_table('custom_file_details');
                frm.refresh_field('custom_file_details');
            }
            if (!frm.doc.items || frm.doc.items.length === 0) {
                let row = frm.add_child('items');
                row.item_code = 'Design Service';
                row.item_name = 'Design Service';
                row.qty = 1;
                row.uom = 'Nos';
                row.rate = 0;
                row.description = 'Custom 3D CAD Design and Modeling Service';
                frm.refresh_field('items');
            }
        }
        toggle_quotation_file_details(frm);
    }

    if (is_ds) {
        apply_type('Sales');
        return;
    }
    if (is_min) {
        apply_type('Miniature Design');
        return;
    }
    if (is_cd) {
        apply_type('Customized 3D');
        return;
    }
    if (is_ps) {
        apply_type('3D Print Service');
        return;
    }

    if (route_order_type) {
        apply_type(route_order_type);
        return;
    }

    if (frm.doc.opportunity) {
        frappe.db.get_value('Opportunity', frm.doc.opportunity, 'opportunity_type').then(r => {
            let opp_type = (r && r.message && r.message.opportunity_type) || '';
            let ot = opp_type.toLowerCase();
            if (ot.includes('miniature') || ot.includes('sculpt')) {
                apply_type('Miniature Design');
            } else if (ot.includes('custom') || ot.includes('cd')) {
                apply_type('Customized 3D');
            } else if (ot.includes('3d print') || ot.includes('printing')) {
                apply_type('3D Print Service');
            } else if (ot.includes('design') || ot.includes('cad')) {
                apply_type('Sales');
            }
        });
    }
}

function set_sales_order_route_options(frm) {
    if (frm.doc.name && frm.doc.docstatus === 1) {
        frappe.route_options = frappe.route_options || {};
        frappe.route_options.quotation = frm.doc.name;
        frappe.route_options.order_type = frm.doc.order_type;
        frappe.route_options.custom_deal_name = frm.doc.custom_deal_name;
        frappe.route_options.custom_google_drive_link = frm.doc.custom_google_drive_link;
    }
}

function toggle_quotation_file_details(frm) {
    let is_file_wf = ['3D Print Service', 'Miniature Design', 'Customized 3D'].includes(frm.doc.order_type);
    frm.toggle_display('custom_file_details', is_file_wf);
}


function sync_drive_link_from_opportunity(frm) {
    if (frappe.route_options && frappe.route_options.custom_google_drive_link && !frm.doc.custom_google_drive_link) {
        frm.set_value('custom_google_drive_link', frappe.route_options.custom_google_drive_link);
        return;
    }
    if (frm.doc.opportunity && !frm.doc.custom_google_drive_link) {
        frappe.db.get_value('Opportunity', frm.doc.opportunity, 'custom_google_drive_link').then(r => {
            if (r && r.message && r.message.custom_google_drive_link) {
                frm.set_value('custom_google_drive_link', r.message.custom_google_drive_link);
            }
        });
    } else if (frm.doc.party_name && !frm.doc.custom_google_drive_link) {
        frappe.db.get_value('Lead', frm.doc.party_name, 'custom_google_drive_link').then(r => {
            if (r && r.message && r.message.custom_google_drive_link) {
                frm.set_value('custom_google_drive_link', r.message.custom_google_drive_link);
            }
        });
    }
}

function sync_deal_name_from_opportunity(frm) {
    if (frm.doc.opportunity && !frm.doc.custom_deal_name) {
        frappe.db.get_value('Opportunity', frm.doc.opportunity, 'custom_deal_name').then(r => {
            if (r && r.message && r.message.custom_deal_name) {
                frm.set_value('custom_deal_name', r.message.custom_deal_name);
            }
        });
    }
}

function sync_files_from_opportunity(frm) {
    if (!frm.is_new() || !frm.doc.opportunity) return;
    if (frm.doc.custom_file_details && frm.doc.custom_file_details.length > 0) return;

    frappe.db.get_doc('Opportunity', frm.doc.opportunity).then(opp => {
        if (!opp || !opp.custom_file_details || opp.custom_file_details.length === 0) return;
        opp.custom_file_details.forEach(row => {
            let new_row = frm.add_child('custom_file_details');
            new_row.file_name = row.file_name;
            new_row.weight = row.weight || 0;
            new_row.support_weight = row.support_weight || 0;
            new_row.dimensions = row.dimensions || row.description || '';
            new_row.description = row.description || '';
            new_row.material_general = row.material_general || 'PLA';
            new_row.material_item = row.material_item;
            new_row.custom_finished_good = row.custom_finished_good;
            new_row.cost_per_gram = row.cost_per_gram || 0;
            new_row.quantity = row.quantity || 1;
            new_row.total = row.total || 0;
            new_row.file_url = row.file_url || '';
        });
        frm.refresh_field('custom_file_details');
        sync_quotation_items_table(frm);
    });
}

async function open_raven_chat(frm) {
    if (!frm || !frm.doc) return;

    let deal_name = frm.doc.custom_deal_name;

    // Fallbacks to find deal_name across doctypes
    if (!deal_name) {
        if (frm.doctype === 'Quotation' && frm.doc.opportunity) {
            try {
                let r = await frappe.db.get_value('Opportunity', frm.doc.opportunity, 'custom_deal_name');
                if (r && r.message && r.message.custom_deal_name) {
                    deal_name = r.message.custom_deal_name;
                    frm.set_value('custom_deal_name', deal_name);
                }
            } catch (e) {}
        } else if (frm.doctype === 'Sales Order' && (frm.doc.opportunity || frm.doc.quotation)) {
            try {
                if (frm.doc.opportunity) {
                    let r = await frappe.db.get_value('Opportunity', frm.doc.opportunity, 'custom_deal_name');
                    if (r && r.message && r.message.custom_deal_name) {
                        deal_name = r.message.custom_deal_name;
                        frm.set_value('custom_deal_name', deal_name);
                    }
                } else if (frm.doc.quotation) {
                    let r = await frappe.db.get_value('Quotation', frm.doc.quotation, 'custom_deal_name');
                    if (r && r.message && r.message.custom_deal_name) {
                        deal_name = r.message.custom_deal_name;
                        frm.set_value('custom_deal_name', deal_name);
                    }
                }
            } catch (e) {}
        } else if (['Delivery Note', 'Sales Invoice'].includes(frm.doctype) && frm.doc.items && frm.doc.items.length > 0) {
            let so = frm.doc.items.map(i => i.against_sales_order || i.sales_order).filter(Boolean)[0];
            if (so) {
                try {
                    let r = await frappe.db.get_value('Sales Order', so, 'custom_deal_name');
                    if (r && r.message && r.message.custom_deal_name) {
                        deal_name = r.message.custom_deal_name;
                        frm.set_value('custom_deal_name', deal_name);
                    }
                } catch (e) {}
            }
        }
    }

    if (!deal_name) {
        deal_name = frm.doc.deal_name || frm.doc.name;
    }

    if (!deal_name) {
        frappe.msgprint(__('Please set the Deal Name before opening chat.'));
        return;
    }

    let clean_name = (deal_name || '').trim().toLowerCase();
    let slug_name = clean_name.replace(/[^a-z0-9_-]/g, '_');
    let channel_id = 'Raven-' + slug_name;
    let user = frappe.session.user;
    let is_admin = (user === 'Administrator' || user === 'protovalue3d@gmail.com');

    let chat_window = window.open('', '_blank');
    if (chat_window) {
        chat_window.document.write('<!DOCTYPE html><html><head><title>Raven Chat</title><style>body{display:flex;align-items:center;justify-content:center;height:100vh;margin:0;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;background:#f8f9fa;color:#333;}.card{background:#fff;padding:32px 48px;border-radius:12px;box-shadow:0 4px 20px rgba(0,0,0,0.08);text-align:center;max-width:420px;}.spinner{width:36px;height:36px;border:3px solid #e0e0e0;border-top-color:#2B7FFF;border-radius:50%;animation:spin 0.8s linear infinite;margin:0 auto 16px;}@keyframes spin{to{transform:rotate(360deg);}}h3{margin:0 0 8px;font-size:18px;font-weight:600;color:#1A1A2E;}p{margin:0;color:#666;font-size:13px;word-break:break-word;}</style></head><body><div class="card"><div class="spinner"></div><h3>Connecting to Raven Chat</h3><p>Loading deal channel: <b>' + frappe.utils.escape_html(deal_name) + '</b></p></div></body></html>');
    }

    function redirect_to_channel() {
        let final_url = `/raven/Raven/${encodeURIComponent(channel_id)}`;
        if (chat_window && !chat_window.closed) {
            chat_window.location.href = final_url;
            chat_window.focus();
        } else {
            window.open(final_url, '_blank');
        }
    }

    function close_and_deny() {
        if (chat_window && !chat_window.closed) {
            chat_window.close();
        }
        frappe.msgprint({
            title: __('Raven Chat'),
            indicator: 'orange',
            message: __('You are not a member of this chat.')
        });
    }

    // Admin / Creator: Ensure private channel exists and redirect
    if (is_admin) {
        const adminToken = '8bb5cdb1d2ab502:f9cfff5f7186691';
        try {
            let chResp = await fetch('/api/resource/Raven%20Channel/' + encodeURIComponent(channel_id), {
                method: 'GET',
                headers: { 'Authorization': 'token ' + adminToken },
                credentials: 'omit'
            });
            if (chResp.status === 404) {
                await fetch('/api/resource/Raven%20Channel', {
                    method: 'POST',
                    headers: {
                        'Authorization': 'token ' + adminToken,
                        'Content-Type': 'application/json'
                    },
                    credentials: 'omit',
                    body: JSON.stringify({
                        doctype: 'Raven Channel',
                        channel_name: clean_name,
                        type: 'Private',
                        workspace: 'Raven'
                    })
                });
            }
        } catch (e) {
            console.warn('Channel check/create notice:', e);
        }
        redirect_to_channel();
        return;
    }

    // Other users: Only open if already a member of this Raven channel
    frappe.call({
        method: 'frappe.client.get_value',
        args: {
            doctype: 'Raven Channel Member',
            fieldname: 'name',
            filters: {
                channel_id: channel_id,
                user_id: user
            }
        },
        silent: true,
        callback: function(r) {
            if (r && r.message && r.message.name) {
                // User is a member of the channel
                redirect_to_channel();
            } else {
                // User is NOT a member: deny access
                close_and_deny();
            }
        },
        error: function() {
            close_and_deny();
        }
    });
}


function flush_email_queue_for_doc(frm) {
    if (!frm.docname) return;
    frappe.db.get_list('Email Queue', {
        filters: {
            reference_doctype: frm.doctype,
            reference_name: frm.docname,
            status: ['in', ['Not Sent', 'Sending']]
        },
        fields: ['name']
    }).then(queue_list => {
        if (!queue_list || queue_list.length === 0) {
            frappe.show_alert({ message: __('All emails for this document have been sent!'), indicator: 'green' });
            return;
        }
        frappe.show_alert({ message: __('Sending ' + queue_list.length + ' queued email(s) immediately...'), indicator: 'blue' });
        queue_list.forEach(q => {
            frappe.call({
                method: 'frappe.email.doctype.email_queue.email_queue.send_now',
                args: { name: q.name },
                callback: function() {
                    frappe.show_alert({ message: __('Email dispatched immediately!'), indicator: 'green' });
                }
            });
        });
    }).catch(err => {
        console.warn('Queue query notice:', err);
    });
}

function hook_email_doc_template(frm) {
    if (!frm || frm._email_doc_auto_hooked) return;
    frm._email_doc_auto_hooked = true;

    let orig_email_doc = frm.email_doc;
    frm.email_doc = function() {
        let composer = orig_email_doc.apply(this, arguments);

        setTimeout(() => {
            try {
                if (!composer || !composer.dialog) return;

                // 1. Pre-select info@protovalue3d.in as sender
                if (composer.dialog.fields_dict.sender) {
                    let opts = composer.dialog.fields_dict.sender.df.options || [];
                    if (opts.includes('info@protovalue3d.in')) {
                        composer.dialog.set_value('sender', 'info@protovalue3d.in');
                    }
                }

                // 2. Identify default template
                let tpl = composer.dialog.get_value('email_template');
                if (!tpl && frm.meta) {
                    tpl = frm.meta.default_email_template;
                }
                if (!tpl) {
                    if (frm.doctype === 'Delivery Note') tpl = 'Dispatch Notification';
                    else if (frm.doctype === 'Quotation') tpl = 'ProtoValue3D Quotation';
                    else if (frm.doctype === 'Sales Invoice') tpl = 'ProtoValue3D Invoice';
                }

                if (tpl) {
                    composer.dialog.set_value('email_template', tpl);

                    // Auto-attach document PDF format
                    if (composer.dialog.has_field('attach_document_print')) {
                        composer.dialog.set_value('attach_document_print', 1);
                        let dpf = (frm.meta && frm.meta.default_print_format);
                        if (!dpf) {
                            if (frm.doctype === 'Delivery Note') dpf = 'ProtoValue3D Delivery Note';
                            else if (frm.doctype === 'Quotation') dpf = 'ProtoValue3D Quotation';
                            else if (frm.doctype === 'Sales Invoice') dpf = 'ProtoValue3D Invoice';
                        }
                        if (dpf && composer.dialog.has_field('select_print_format')) {
                            composer.dialog.set_value('select_print_format', dpf);
                            $(composer.dialog.fields_dict.select_print_format.wrapper).show();
                        }
                    }

                    // 3. Fetch rendered template and populate subject & body
                    frappe.call({
                        method: 'frappe.email.doctype.email_template.email_template.get_email_template',
                        args: {
                            template_name: tpl,
                            doc: frm.doc,
                            sender: 'info@protovalue3d.in'
                        },
                        callback: function(res) {
                            if (res && res.message) {
                                if (composer.dialog.fields_dict.use_html) {
                                    composer.dialog.set_value('use_html', 1);
                                }
                                if (res.message.subject) {
                                    composer.dialog.set_value('subject', res.message.subject);
                                }

                                setTimeout(() => {
                                    let html = res.message.message || '';
                                    if (composer.dialog.fields_dict.html_content) {
                                        composer.dialog.fields_dict.html_content.set_value(html);
                                    }
                                    if (composer.dialog.fields_dict.content) {
                                        composer.dialog.fields_dict.content.set_value(html);
                                    }
                                    let cf = composer.get_content_field();
                                    if (cf) cf.set_value(html);
                                }, 150);
                            }
                        }
                    });
                }
            } catch (err) {
                console.error('Error auto-filling email template:', err);
            }
        }, 300);

        return composer;
    };
}

function setup_auto_email_sender() {
    if (frappe.views && frappe.views.CommunicationComposer) {
        // Auto-send hook on send_action: triggers immediate dispatch and shows green delivery alert
        if (!frappe.views.CommunicationComposer.prototype._auto_send_now_patched) {
            frappe.views.CommunicationComposer.prototype._auto_send_now_patched = true;
            let orig_send_action = frappe.views.CommunicationComposer.prototype.send_action;
            frappe.views.CommunicationComposer.prototype.send_action = function() {
                let doc_type = this.doc && this.doc.doctype;
                let doc_name = this.doc && this.doc.name;
                orig_send_action.apply(this, arguments);

                if (doc_type && doc_name) {
                    setTimeout(() => {
                        frappe.db.get_list('Email Queue', {
                            filters: {
                                reference_doctype: doc_type,
                                reference_name: doc_name,
                                status: ['in', ['Not Sent', 'Sending']]
                            },
                            fields: ['name']
                        }).then(records => {
                            if (records && records.length > 0) {
                                records.forEach(item => {
                                    frappe.call({
                                        method: 'frappe.email.doctype.email_queue.email_queue.send_now',
                                        args: { name: item.name },
                                        callback: function() {
                                            frappe.show_alert({ message: __('Email dispatched immediately!'), indicator: 'green' });
                                        }
                                    });
                                });
                            }
                        }).catch(() => {});
                    }, 1200);
                }
            };
        }

        // Global prototype hook on CommunicationComposer.prototype.set_values to pre-fill template & PDF
        if (!frappe.views.CommunicationComposer.prototype._auto_template_hook_v2) {
            frappe.views.CommunicationComposer.prototype._auto_template_hook_v2 = true;
            let orig_set_values = frappe.views.CommunicationComposer.prototype.set_values;
            frappe.views.CommunicationComposer.prototype.set_values = async function() {
                await orig_set_values.apply(this, arguments);

                if (this.is_a_reply) return;

                let self = this;
                let dt = (this.frm && this.frm.doctype) || (this.doc && this.doc.doctype);
                let tpl = this.dialog.get_value("email_template");
                if (!tpl && this.frm && this.frm.meta) {
                    tpl = this.frm.meta.default_email_template;
                }
                if (!tpl) {
                    if (dt === 'Delivery Note') tpl = 'Dispatch Notification';
                    else if (dt === 'Quotation') tpl = 'ProtoValue3D Quotation';
                    else if (dt === 'Sales Invoice') tpl = 'ProtoValue3D Invoice';
                }

                if (tpl) {
                    if (this.dialog.fields_dict.sender) {
                        let opts = this.dialog.fields_dict.sender.df.options || [];
                        if (opts.includes('info@protovalue3d.in')) {
                            await this.dialog.set_value('sender', 'info@protovalue3d.in');
                        }
                    }

                    if (!this.dialog.get_value("email_template")) {
                        await this.dialog.set_value("email_template", tpl);
                    }

                    // Auto-attach document PDF format
                    if (this.dialog.has_field("attach_document_print") && !this.dialog.get_value("attach_document_print")) {
                        let dpf = (this.frm && this.frm.meta && this.frm.meta.default_print_format);
                        if (!dpf) {
                            if (dt === 'Delivery Note') dpf = 'ProtoValue3D Delivery Note';
                            else if (dt === 'Quotation') dpf = 'ProtoValue3D Quotation';
                            else if (dt === 'Sales Invoice') dpf = 'ProtoValue3D Invoice';
                        }
                        if (dpf) {
                            await this.dialog.set_value("attach_document_print", 1);
                            if (this.dialog.has_field("select_print_format")) {
                                await this.dialog.set_value("select_print_format", dpf);
                                $(this.dialog.fields_dict.select_print_format.wrapper).show();
                            }
                        }
                    }

                    // Auto-populate template HTML message and subject into composer
                    let content_field = self.get_content_field();
                    let current_content = (content_field ? (content_field.get_value() || '').trim() : '');
                    let already_has = current_content.includes('protovalue3d-logo.png') || current_content.includes(tpl);

                    if (!already_has) {
                        frappe.call({
                            method: "frappe.email.doctype.email_template.email_template.get_email_template",
                            args: {
                                template_name: tpl,
                                doc: self.doc,
                                sender: "info@protovalue3d.in"
                            },
                            silent: true,
                            callback: function(r) {
                                if (r && r.message) {
                                    if (self.dialog.fields_dict.use_html) {
                                        self.dialog.set_value("use_html", 1);
                                    }
                                    if (r.message.subject) {
                                        self.dialog.set_value("subject", r.message.subject);
                                    }
                                    setTimeout(() => {
                                        let html = r.message.message || '';
                                        if (self.dialog.fields_dict.html_content) self.dialog.fields_dict.html_content.set_value(html);
                                        if (self.dialog.fields_dict.content) self.dialog.fields_dict.content.set_value(html);
                                        let cf = self.get_content_field();
                                        if (cf) cf.set_value(html);
                                    }, 150);
                                }
                            },
                            error: function() {}
                        });
                    }
                }
            };
        }
    }
}


function create_and_open_drive_folder(frm) {
    if (frm.doc.custom_google_drive_link) {
        window.open(frm.doc.custom_google_drive_link, '_blank');
        return;
    }

    let deal = (frm.doc.custom_deal_name || '').trim();
    if (!deal) {
        if (frm.doc.order_type === 'Display Parts') {
            ensure_custom_deal_name(frm);
            deal = frm.doc.custom_deal_name || 'DP_001';
        } else {
            deal = frm.doc.name || 'Order';
        }
    }

    // Open blank window immediately to prevent browser popup blockers
    let newTab = window.open('about:blank', '_blank');

    frappe.show_alert({
        message: __('Creating Google Drive folder for <b>' + deal + '</b>...'),
        indicator: 'blue'
    });

    fetch('https://www.protovalue3d.in/api/gdrive/folder', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
            action: 'create',
            name: deal
        })
    })
    .then(r => r.json())
    .then(data => {
        if (data.success && (data.folderUrl || data.folderId)) {
            let url = data.folderUrl || ('https://drive.google.com/drive/folders/' + data.folderId);
            frm.set_value('custom_google_drive_link', url);

            if (newTab) {
                newTab.location.href = url;
            } else {
                window.open(url, '_blank');
            }

            frm.save().then(() => {
                frappe.show_alert({
                    message: __('Google Drive folder created & opened: ' + deal),
                    indicator: 'green'
                });
            });
        } else {
            if (newTab) newTab.close();
            frappe.msgprint({
                title: __('Folder Creation Failed'),
                indicator: 'red',
                message: data.message || 'Could not auto-create Google Drive folder.'
            });
        }
    })
    .catch(err => {
        if (newTab) newTab.close();
        frappe.msgprint({
            title: __('Error'),
            indicator: 'red',
            message: __('Failed to connect to Google Drive service: ' + err.message)
        });
    });
}
