// Helper to resolve Finished Good code from raw material
function resolve_fg_for_row(cdt, cdn) {
    let row = locals[cdt][cdn];
    if (!row.material_item) {
        frappe.model.set_value(cdt, cdn, 'custom_finished_good', '');
        return Promise.resolve('');
    }
    return frappe.db.get_value('Item', row.material_item, ['custom_finished_good_item', 'item_group', 'item_name']).then(r => {
        let d = (r && r.message) || {};
        let fg = d.custom_finished_good_item;
        if (!fg) {
            let str = (row.material_item + ' ' + (d.item_group || '') + ' ' + (d.item_name || '')).toUpperCase();
            if (str.includes('PETG')) fg = 'FG_FDM_PETG';
            else if (str.includes('ABS')) fg = 'FG_FDM_ABS';
            else if (str.includes('RESIN') || str.includes('SLA')) fg = 'FG_SLA_RESIN';
            else fg = 'FG_FDM_PLA';
        }
        return frappe.model.set_value(cdt, cdn, 'custom_finished_good', fg);
    });
}

// 1. Service Nomenclature and Helpers
function get_service_nomenclature(service_type) {
    if (!service_type) return 'PS';
    let s = (service_type || '').toLowerCase();
    if (s.includes('design service') || s === 'design' || s.includes('cad') || s.includes('2d to 3d')) {
        return 'DS';
    }
    if (s.includes('miniature') || s.includes('sculpt')) {
        return 'Min';
    }
    if (s.includes('custom') || s.includes('cd')) {
        return 'CD';
    }
    if (s.includes('product development') || s.includes('npd')) {
        return 'NPD';
    }
    if (s.includes('printing') || s.includes('print')) {
        return 'PS';
    }
    return 'PS';
}

function clean_deal_name_part(name) {
    if (!name) return 'customer';
    return name.toLowerCase().replace(/[^a-z0-9]/g, '');
}

function get_deal_date_code() {
    let now = new Date();
    let dd = String(now.getDate()).padStart(2, '0');
    let mm = String(now.getMonth() + 1).padStart(2, '0');
    let yy = String(now.getFullYear()).slice(-2);
    return `${dd}${mm}${yy}`;
}

function generate_and_set_deal_name(frm, raw_name, service_type) {
    let clean_party = clean_deal_name_part(raw_name);
    let service_code = get_service_nomenclature(service_type);
    let date_code = get_deal_date_code();
    let prefix = `${clean_party}_${service_code}_${date_code}_`;

    frappe.db.get_list('Opportunity', {
        filters: [['custom_deal_name', 'like', prefix + '%']],
        fields: ['custom_deal_name', 'name'],
        limit_page_length: 50
    }).then(res => {
        let max_serial = 0;
        (res || []).forEach(row => {
            if (row.name === frm.doc.name) return;
            let val = row.custom_deal_name || '';
            let sl_str = val.substring(prefix.length);
            let sl = parseInt(sl_str);
            if (!isNaN(sl) && sl > max_serial) {
                max_serial = sl;
            }
        });
        let next_serial = max_serial + 1;
        let generated_deal_name = `${prefix}${next_serial}`;
        frm.set_value('custom_deal_name', generated_deal_name);
    });
}

function is_file_table_service(type) {
    if (!type) return false;
    return ['3D Print Service', 'Miniature Design', 'Customized 3D'].includes(type);
}

function get_mapped_opportunity_type(lead_type) {
    if (!lead_type) return '3D Print Service';
    let lt = (lead_type || '').toLowerCase();
    if (lt.includes('miniature') || lt.includes('sculpt')) return 'Miniature Design';
    if (lt.includes('3d print') || lt.includes('printing')) return '3D Print Service';
    if (lt.includes('product development') || lt.includes('npd')) return 'Product Development';
    if (lt.includes('cad') || (lt.includes('design') && !lt.includes('model') && !lt.includes('gift'))) return 'Design Service';
    if (lt.includes('custom') || lt.includes('cd')) return 'Customized 3D';
    if (lt.includes('design') || lt.includes('2d to 3d')) return 'Design Service';
    return lead_type;
}

function extract_lead_enquiry_quantity(note_text) {
    if (!note_text) return 1;
    // 1. Matches "Total Parts Quantity: 3" or "Quantity: 3" or "Quantity Required: 3"
    let m = note_text.match(/(?:Total Parts Quantity|Quantity Required|Quantity)\s*[:|=]\s*(\d+)/i);
    if (m && m[1]) {
        let q = parseInt(m[1], 10);
        if (!isNaN(q) && q > 0) return q;
    }
    // 2. Matches "| Qty: 3 |" or "Qty: 3"
    let m2 = note_text.match(/(?:\||\b)\s*Qty\s*[:|=]\s*(\d+)/i);
    if (m2 && m2[1]) {
        let q = parseInt(m2[1], 10);
        if (!isNaN(q) && q > 0) return q;
    }
    return 1;
}

function extract_lead_enquiry_product(note_text) {
    if (!note_text) return null;
    let m = note_text.match(/Product Requested:\s*([^\r\n]+)/i);
    if (m && m[1]) return m[1].trim();
    let m2 = note_text.match(/Item:\s*([^|\r\n]+)/i);
    if (m2 && m2[1]) return m2[1].trim();
    return null;
}

function extract_drive_files_from_note(note_text) {
    if (!note_text) return [];
    let files = [];
    let drive_file_regex = /-\s*([^:\r\n]+?)\s*:\s*(https?:\/\/[^\s\r\n]+)/gi;
    let match;
    while ((match = drive_file_regex.exec(note_text)) !== null) {
        let fn = match[1].trim();
        let url = match[2].trim();
        if (fn && url) {
            files.push({ file_name: fn, file_url: url });
        }
    }
    return files;
}


// 2. Auto-sync from Lead or Customer to Opportunity
function sync_lead_data_to_opportunity(frm) {
    if (frm.doctype !== 'Opportunity') return;

    if (frm.doc.opportunity_from === 'Lead' && frm.doc.party_name) {
        frappe.db.get_doc('Lead', frm.doc.party_name).then(lead => {
            if (!lead) return;

            let mapped_type = get_mapped_opportunity_type(lead.type);
            if (lead.type && (!frm.doc.opportunity_type || frm.is_new())) {
                frm.set_value('opportunity_type', mapped_type);
            }

            if (lead.custom_google_drive_link && !frm.doc.custom_google_drive_link) {
                frm.set_value('custom_google_drive_link', lead.custom_google_drive_link);
            }

            if (!frm.doc.custom_deal_name || frm.is_new()) {
                generate_and_set_deal_name(frm, lead.lead_name || lead.name, mapped_type || lead.type);
            }

            let note_text = (lead.notes || []).map(n => n.note || '').join('\n');
            let enquiry_qty = extract_lead_enquiry_quantity(note_text);
            let enquiry_prod = extract_lead_enquiry_product(note_text);
            frm._enquiry_qty = enquiry_qty;
            frm._enquiry_prod = enquiry_prod;

            let file_wf = is_file_table_service(frm.doc.opportunity_type || mapped_type || lead.type);
            if (file_wf && (!frm.doc.custom_file_details || frm.doc.custom_file_details.length === 0)) {
                populate_files_from_note(frm, note_text, enquiry_qty, enquiry_prod);
            }

            handle_service_type_setup(frm, enquiry_qty, enquiry_prod);
        });
    } else if (frm.doc.opportunity_from === 'Customer' && frm.doc.party_name) {
        frappe.db.get_value('Customer', frm.doc.party_name, ['customer_name', 'lead_name']).then(r => {
            let d = (r && r.message) || {};
            let cust_name = d.customer_name || frm.doc.party_name;
            if (!frm.doc.custom_deal_name || frm.is_new()) {
                generate_and_set_deal_name(frm, cust_name, frm.doc.opportunity_type);
            }
            if (d.lead_name && !frm.doc.custom_google_drive_link) {
                frappe.db.get_value('Lead', d.lead_name, 'custom_google_drive_link').then(lr => {
                    if (lr && lr.message && lr.message.custom_google_drive_link) {
                        frm.set_value('custom_google_drive_link', lr.message.custom_google_drive_link);
                    }
                });
            }
        });
        handle_service_type_setup(frm);
    } else {
        handle_service_type_setup(frm);
    }
}

// 3. Auto-populate file rows from enquiry notes if files were attached
function populate_files_from_note(frm, note_text, enquiry_qty, enquiry_prod) {
    if (!note_text) return;
    
    let default_qty = enquiry_qty || frm._enquiry_qty || 1;
    let count = 0;

    // Split note into individual file blocks: "1. File: ...", "2. File: ..."
    let file_blocks = note_text.split(/(?:^|\n)\s*(?=\d+\.\s*File:)/i);

    file_blocks.forEach(block => {
        let name_match = block.match(/(?:^|\n)\s*\d+\.\s*File:\s*([^\r\n]+)/i);
        if (!name_match) return;

        let f_name = name_match[1].trim();
        let mat_match = block.match(/-\s*Material:\s*([^\r\n]+)/i);
        let f_mat = mat_match ? mat_match[1].trim() : 'PLA';

        let qty_match = block.match(/-\s*Quantity:\s*(\d+)/i);
        let f_qty = qty_match ? parseInt(qty_match[1].trim()) : default_qty;

        let weight_match = block.match(/-\s*Weight:\s*([\d.]+)/i);
        let f_weight = weight_match ? parseFloat(weight_match[1].trim()) : 0;

        let support_match = block.match(/-\s*Support Weight:\s*([\d.]+)/i);
        let f_support = support_match ? parseFloat(support_match[1].trim()) : 0;

        let dims_match = block.match(/-\s*Dimensions:\s*([^\r\n]+)/i);
        let f_dims = dims_match ? dims_match[1].trim() : '';

        let url_match = block.match(/-\s*File URL:\s*(https?:\/\/[^\s\r\n]+)/i);
        let f_url = url_match ? url_match[1].trim() : '';

        let row = frm.add_child('custom_file_details');
        row.file_name = f_name;
        row.material_general = f_mat;
        row.quantity = f_qty;
        row.weight = f_weight;
        row.support_weight = f_support;
        if (f_dims) {
            row.dimensions = f_dims;
        }
        if (f_url) row.file_url = f_url;
        row.cost_per_gram = 0;
        row.total = (flt(row.weight) + flt(row.support_weight)) * flt(row.cost_per_gram) * flt(row.quantity);
        count++;
    });

    if (count === 0) {
        let file_regex = /(?:^|\n)\s*\d+\.\s*File:\s*([^\r\n]+)[\s\S]*?-\s*Material:\s*([^\r\n]+)[\s\S]*?-\s*Quantity:\s*(\d+)/gi;
        let match;
        while ((match = file_regex.exec(note_text)) !== null) {
            let f_name = match[1].trim();
            let f_mat = match[2].trim();
            let f_qty = parseInt(match[3].trim()) || default_qty;

            let row = frm.add_child('custom_file_details');
            row.file_name = f_name;
            row.material_general = f_mat;
            row.quantity = f_qty;
            row.weight = 0;
            row.support_weight = 0;
            row.cost_per_gram = 0;
            row.total = 0;
            count++;
        }
    }

    if (count === 0) {
        let pipe_regex = /(?:^|\n)\s*\d+\.\s*([^|\r\n]+?)\s*\|\s*Material:\s*([^|\r\n]+?)\s*(?:\|[^\r\n]*?)?\|\s*Qty:\s*(\d+)/gi;
        let match2;
        while ((match2 = pipe_regex.exec(note_text)) !== null) {
            let f_name = match2[1].trim();
            let f_mat = match2[2].trim();
            let f_qty = parseInt(match2[3].trim()) || default_qty;

            let row = frm.add_child('custom_file_details');
            row.file_name = f_name;
            row.material_general = f_mat;
            row.quantity = f_qty;
            row.weight = 0;
            row.support_weight = 0;
            row.cost_per_gram = 0;
            row.total = 0;
            count++;
        }
    }

    // 3. Drive files fallback (e.g. for Customized 3D or image uploads)
    if (count === 0) {
        let drive_files = extract_drive_files_from_note(note_text);
        if (drive_files.length > 0) {
            drive_files.forEach(df => {
                let row = frm.add_child('custom_file_details');
                row.file_name = df.file_name;
                row.file_url = df.file_url;
                row.material_general = 'PLA';
                row.material_item = 'JMG_PLA_B';
                row.quantity = default_qty;
                row.weight = 0;
                row.support_weight = 0;
                row.cost_per_gram = 0;
                row.total = 0;
                count++;
            });
        }
    }

    if (count > 0) {
        frm.refresh_field('custom_file_details');
        frappe.show_alert({
            message: __('Imported {0} CAD file(s) with weights from Lead enquiry into File Details table', [count]),
            indicator: 'green'
        });
    }
}

// 4. File-row interactions: Material selection, rate fetch, total recalculation
frappe.ui.form.on('Print file detail', {
    material_item: function(frm, cdt, cdn) {
        let row = locals[cdt][cdn];
        resolve_fg_for_row(cdt, cdn);
        if (!row.material_item) {
            frappe.model.set_value(cdt, cdn, 'cost_per_gram', 0);
            return;
        }

        // For Miniature Design, printing cost is included in miniature design price, so cost_per_gram is 0 by default
        if (frm.doc.opportunity_type === 'Miniature Design') {
            frappe.model.set_value(cdt, cdn, 'cost_per_gram', 0).then(() => {
                calc_row_total(frm, cdt, cdn);
            });
            return;
        }

        frappe.db.get_list('Item Price', {
            filters: {
                item_code: row.material_item,
                price_list: 'Standard Selling',
                selling: 1
            },
            fields: ['price_list_rate'],
            limit: 1
        }).then(res => {
            if (res && res.length > 0 && res[0].price_list_rate != null && res[0].price_list_rate > 0) {
                let selling_rate = res[0].price_list_rate;
                frappe.model.set_value(cdt, cdn, 'cost_per_gram', selling_rate).then(() => {
                    calc_row_total(frm, cdt, cdn);
                });
            } else {
                frappe.db.get_value('Item', row.material_item, ['standard_rate', 'valuation_rate']).then(r => {
                    let rate = 0;
                    if (r && r.message) {
                        rate = r.message.standard_rate || r.message.valuation_rate || 0;
                    }
                    frappe.model.set_value(cdt, cdn, 'cost_per_gram', rate).then(() => {
                        calc_row_total(frm, cdt, cdn);
                    });
                });
            }
        });
    },
    weight: function(frm, cdt, cdn) { calc_row_total(frm, cdt, cdn); },
    support_weight: function(frm, cdt, cdn) { calc_row_total(frm, cdt, cdn); },
    cost_per_gram: function(frm, cdt, cdn) { calc_row_total(frm, cdt, cdn); },
    quantity: function(frm, cdt, cdn) { calc_row_total(frm, cdt, cdn); },
});

function calc_row_total(frm, cdt, cdn) {
    let row = locals[cdt][cdn];
    let weight = flt(row.weight) || 0;
    let support_weight = flt(row.support_weight) || 0;
    let total_weight = weight + support_weight;
    let cost = flt(row.cost_per_gram) || 0;
    let qty = flt(row.quantity) || 1;
    let total = total_weight * cost * qty;
    
    row.total = total;
    frm.refresh_field('custom_file_details');

    if (frm.doctype === 'Opportunity' && is_file_table_service(frm.doc.opportunity_type)) {
        sync_opportunity_items_table(frm);
    }
}

// 5. Rebuild Opportunity Items table synchronously for Opportunity doctype
function sync_opportunity_items_table(frm) {
    if (frm.doctype !== 'Opportunity') return;
    if (!is_file_table_service(frm.doc.opportunity_type)) return;

    let is_miniature = (frm.doc.opportunity_type === 'Miniature Design');
    let is_custom = (frm.doc.opportunity_type === 'Customized 3D' || (frm.doc.custom_deal_name || '').includes('_CD_'));
    let is_zero_material = (is_miniature || is_custom);
    let conversion_rate = flt(frm.doc.conversion_rate) || 1;

    let file_rows = (frm.doc.custom_file_details || []).filter(r => r.material_item);
    let file_material_codes = new Set(file_rows.map(r => r.material_item));

    // 1. Collect non-material / service rows to preserve
    let preserved_items = [];
    (frm.doc.items || []).forEach(i => {
        let code = (i.item_code || '').toLowerCase();
        let is_service = code.includes('miniature') || code.includes('sculpt') || code.includes('design') || code.includes('cad') || code.includes('custom') || code.includes('gift') || code.includes('keychain') || code.includes('toy') || code.includes('lamp') || code.includes('trophy') || code.includes('organizer') || code.includes('award') || code.includes('topper') || code.includes('nameplate') || code.includes('bobblehead') || code.includes('dragon') || code.includes('collectible') || code.includes('tower') || code.includes('bookend') || code.includes('stand') || code.includes('enclosure') || code.includes('reverse');
        // Preserve if it is a service item OR does not match the file row materials
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
                qty: qty,
                rate: rate,
                amount: amount,
                base_rate: flt(i.base_rate) || (rate * conversion_rate),
                base_amount: flt(i.base_amount) || (amount * conversion_rate),
                conversion_factor: flt(i.conversion_factor) || 1
            });
        }
    });

    // For Miniature Design: if no miniature design item is present, add the default 4inch miniature design
    if (is_miniature) {
        let has_miniature = preserved_items.some(i => (i.item_code || '').toLowerCase().includes('miniature'));
        if (!has_miniature) {
            preserved_items.unshift({
                item_code: '4inch miniature design',
                item_name: '4inch miniature design',
                description: '4 Inch Custom Miniature 3D Design & Sculpting Service',
                uom: 'Nos',
                qty: 1,
                rate: 0,
                amount: 0,
                base_rate: 0,
                base_amount: 0,
                conversion_factor: 1
            });
        }
    }

    // 2. Prepare material items from file rows
    let material_items = [];
    file_rows.forEach(row => {
        let weight = flt(row.weight) || 0;
        let support_weight = flt(row.support_weight) || 0;
        let billed_weight = weight + support_weight;
        let units = parseInt(row.quantity) || 1;
        // In Miniature Design, printing material rate is 0 (cost covered by miniature design item)
        let rate = is_zero_material ? 0 : (flt(row.cost_per_gram) || 0);
        let row_desc = support_weight > 0
            ? `${row.file_name || 'Print Model'} (${row.material_general || ''} - ${row.material_item}) [Part: ${weight}g, Support: ${support_weight}g]`
            : `${row.file_name || 'Print Model'} (${row.material_general || ''} - ${row.material_item})`;

        for (let u = 0; u < units; u++) {
            let amount = billed_weight * rate;
            material_items.push({
                item_code: (row.custom_finished_good || (row.material_item && row.material_item.toUpperCase().includes('PETG') ? 'FG_FDM_PETG' : (row.material_item && row.material_item.toUpperCase().includes('ABS') ? 'FG_FDM_ABS' : (row.material_item && (row.material_item.toUpperCase().includes('RESIN') || row.material_item.toUpperCase().includes('SLA')) ? 'FG_SLA_RESIN' : 'FG_FDM_PLA')))),
                item_name: (row.custom_finished_good || (row.material_item && row.material_item.toUpperCase().includes('PETG') ? 'FG_FDM_PETG' : (row.material_item && row.material_item.toUpperCase().includes('ABS') ? 'FG_FDM_ABS' : (row.material_item && (row.material_item.toUpperCase().includes('RESIN') || row.material_item.toUpperCase().includes('SLA')) ? 'FG_SLA_RESIN' : 'FG_FDM_PLA')))),
                description: row_desc,
                uom: 'Gram',
                qty: billed_weight,
                rate: rate,
                amount: amount,
                base_rate: rate * conversion_rate,
                base_amount: amount * conversion_rate,
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
}

// 6. Form events on Opportunity
frappe.ui.form.on('Opportunity', {
    onload(frm) {
        if (frm.is_new()) {
            if (frappe.route_options) {
                delete frappe.route_options.custom_deal_name;
                delete frappe.route_options.custom_google_drive_link;
            }
            if (!frm.doc.party_name) {
                frm.set_value('custom_deal_name', '');
                frm.set_value('custom_google_drive_link', '');
            }
            if (!frm.doc.opportunity_type) {
                frm.set_value('opportunity_type', '3D Print Service');
            }
        }
        sync_lead_data_to_opportunity(frm);
    },
    refresh(frm) {
        if (frm.is_new() && !frm.doc.party_name) {
            if (frappe.route_options) {
                delete frappe.route_options.custom_deal_name;
                delete frappe.route_options.custom_google_drive_link;
            }
            if (frm.doc.custom_deal_name) frm.set_value('custom_deal_name', '');
            if (frm.doc.custom_google_drive_link) frm.set_value('custom_google_drive_link', '');
        }
        sync_lead_data_to_opportunity(frm);
    },
    opportunity_type(frm) {
        handle_service_type_setup(frm);
        if (frm.doc.party_name && (!frm.doc.custom_deal_name || frm.is_new())) {
            let party_display = frm.doc.customer_name || frm.doc.title || frm.doc.party_name;
            generate_and_set_deal_name(frm, party_display, frm.doc.opportunity_type);
        }
    },
    party_name(frm) {
        sync_lead_data_to_opportunity(frm);
        if (!frm.doc.custom_deal_name && frm.doc.party_name) {
            let party_display = frm.doc.customer_name || frm.doc.title || frm.doc.party_name;
            generate_and_set_deal_name(frm, party_display, frm.doc.opportunity_type);
        }
    },
    opportunity_from(frm) {
        sync_lead_data_to_opportunity(frm);
    },
    custom_file_details_remove(frm) {
        sync_opportunity_items_table(frm);
    },
    validate: function(frm) {
        handle_service_type_setup(frm);
        if (frm.doctype === 'Opportunity' && is_file_table_service(frm.doc.opportunity_type)) {
            sync_opportunity_items_table(frm);
        }
        (frm.doc.items || []).forEach(row => {
            if (!row.uom) row.uom = 'Nos';
            if (!row.stock_uom) row.stock_uom = row.uom || 'Nos';
            if (!row.conversion_factor || flt(row.conversion_factor) === 0) row.conversion_factor = 1.0;
            if (!row.stock_qty) row.stock_qty = (flt(row.qty) || 1) * (flt(row.conversion_factor) || 1);
        });
    }
});

function handle_service_type_setup(frm, enquiry_qty, enquiry_prod) {
    if (frm.doctype !== 'Opportunity') return;

    let target_qty = enquiry_qty || frm._enquiry_qty || 1;
    let prod_title = enquiry_prod || frm._enquiry_prod || null;

    let ot = frm.doc.opportunity_type;
    let is_file_wf = is_file_table_service(ot) || ((frm.doc.custom_file_details || []).length > 0);
    let is_design = (ot === 'Design Service');
    let is_miniature = (ot === 'Miniature Design');
    let is_custom_3d = (ot === 'Customized 3D');

    // 1. Files table display: VISIBLE for 3D Print, Miniature Design, Customized 3D; HIDDEN for Design Service
    frm.toggle_display('custom_file_details', is_file_wf);

    // 2. Setup route options for native Quotation creation
    frappe.route_options = frappe.route_options || {};
    if (is_design) {
        frappe.route_options.order_type = 'Sales';
    } else if (is_miniature) {
        frappe.route_options.order_type = 'Miniature Design';
    } else if (is_custom_3d) {
        frappe.route_options.order_type = 'Customized 3D';
    } else if (ot === '3D Print Service') {
        frappe.route_options.order_type = '3D Print Service';
    } else {
        frappe.route_options.order_type = 'Sales';
    }

// route options for deal_name/drive_link removed to prevent cross-doc pollution

    // 3. For Design Service: ensure 'Design Service' item is present, with carried quantity
    if (is_design) {
        if (frm.doc.custom_file_details && frm.doc.custom_file_details.length > 0) {
            frm.clear_table('custom_file_details');
            frm.refresh_field('custom_file_details');
        }
        let design_row = (frm.doc.items || []).find(i => (i.item_code || '').toLowerCase().includes('design'));
        if (!design_row) {
            let row = frm.add_child('items');
            row.item_code = 'Design Service';
            row.item_name = 'Design Service';
            row.qty = target_qty;
            row.uom = 'Nos';
            row.rate = 0;
            row.amount = 0;
            row.base_rate = 0;
            row.base_amount = 0;
            row.conversion_factor = 1;
            row.description = frm.doc.description || 'Custom 3D CAD Design and Modeling Service';
            frm.refresh_field('items');
        } else if ((frm.is_new() || design_row.qty === 1) && target_qty > 1) {
            design_row.qty = target_qty;
            frm.refresh_field('items');
        }
    }

    // 4. For Miniature Design: ensure a default miniature design item is present, with carried quantity
    if (is_miniature) {
        let min_row = (frm.doc.items || []).find(i => (i.item_code || '').toLowerCase().includes('miniature'));
        if (!min_row) {
            let row = frm.add_child('items');
            row.item_code = '4inch miniature design';
            row.item_name = '4inch miniature design';
            row.qty = target_qty;
            row.uom = 'Nos';
            row.rate = 0;
            row.amount = 0;
            row.base_rate = 0;
            row.base_amount = 0;
            row.conversion_factor = 1;
            row.description = '4 Inch Custom Miniature 3D Design & Sculpting Service';
            frm.refresh_field('items');
        } else if ((frm.is_new() || min_row.qty === 1) && target_qty > 1) {
            min_row.qty = target_qty;
            frm.refresh_field('items');
        }
    }

    // 5. For Customized 3D: ensure custom 3D model item is present with carried quantity
    if (is_custom_3d) {
        let custom_row = (frm.doc.items || []).find(i => {
            let code = (i.item_code || '').toLowerCase();
            return code.includes('custom') || code.includes('gift') || code.includes('keychain') || code.includes('toy') || code.includes('lamp') || code.includes('trophy') || code.includes('design');
        });
        let item_title = prod_title || 'Customised 3D Models & Gifts';
        let item_desc = prod_title ? `Custom 3D Model: ${prod_title}` : 'Bespoke Custom 3D Modeling, Prototyping, and Personalized Gift Design Service';

        if (!custom_row) {
            let row = frm.add_child('items');
            row.item_code = 'Customised 3D Models & Gifts';
            row.item_name = item_title;
            row.qty = target_qty;
            row.uom = 'Nos';
            row.rate = 0;
            row.amount = 0;
            row.base_rate = 0;
            row.base_amount = 0;
            row.conversion_factor = 1;
            row.description = item_desc;
            frm.refresh_field('items');
        } else if ((frm.is_new() || custom_row.qty === 1) && target_qty > 1) {
            custom_row.qty = target_qty;
            if (prod_title && custom_row.item_code === 'Customised 3D Models & Gifts') {
                custom_row.item_name = item_title;
                custom_row.description = item_desc;
            }
            frm.refresh_field('items');
        }
    }
}