frappe.ui.form.on('Sales Order', {
    onload(frm) {
        ensure_custom_deal_name(frm);
        sync_files_from_quotation(frm);
        sync_shop_items_from_bom(frm, false);
        toggle_order_type_view(frm);
        sync_google_drive_link_from_source(frm);
    },

    refresh(frm) {
        // Google Drive Folder Button under Create (auto-create) or Actions (to open)
        if (!frm.doc.custom_google_drive_link) {
            frm.add_custom_button(__('Google Drive Folder'), () => {
                if (frm.is_new()) {
                    ensure_custom_deal_name(frm);
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


        setup_auto_email_sender();
        hook_email_doc_template(frm);
        frm.add_custom_button(__('Flush / Send Queued Email'), () => {
            flush_email_queue_for_doc(frm);
        }, __('Actions'));
        // Action button to manually unpack BOM to print queue
        frm.add_custom_button(__('Unpack BOM to Print Queue'), () => {
            unpack_bom_items_interactive(frm);
        }, __('Actions'));



        if (frm.fields_dict && frm.fields_dict['custom_file_details'] && frm.fields_dict['custom_file_details'].grid) {
            frm.fields_dict['custom_file_details'].grid.toggle_display('description', true);
        }

        // Dispatch Tag Button under Create
        frm.add_custom_button(__('Dispatch Tag'), function () {
            let deal = (frm.doc.custom_deal_name || frm.doc.name || 'Deal').trim().replace(/[^a-zA-Z0-9._-]/g, '_');
            let target_filename = deal + '_DispatchTag.pdf';
            let url = '/api/method/frappe.utils.print_format.download_pdf?doctype=Sales Order&name=' + encodeURIComponent(frm.docname) + '&format=Dispatch Tag&no_letterhead=1';

            frappe.show_alert({ message: __('Downloading ' + target_filename + '...'), indicator: 'blue' });

            fetch(url, { headers: { 'X-Frappe-CSRF-Token': frappe.csrf_token } })
                .then(res => {
                    if (!res.ok) throw new Error('Download failed: ' + res.statusText);
                    return res.blob();
                })
                .then(blob => {
                    // Direct browser file download with dealname_DispatchTag.pdf
                    let blobUrl = window.URL.createObjectURL(blob);
                    let a = document.createElement('a');
                    a.style.display = 'none';
                    a.href = blobUrl;
                    a.download = target_filename;
                    document.body.appendChild(a);
                    a.click();
                    setTimeout(() => {
                        document.body.removeChild(a);
                        window.URL.revokeObjectURL(blobUrl);
                    }, 200);

                    // Also attach to Sales Order sidebar
                    let formData = new FormData();
                    formData.append('file', blob, target_filename);
                    formData.append('doctype', frm.doctype);
                    formData.append('docname', frm.docname);
                    formData.append('is_private', 1);

                    fetch('/api/method/upload_file', {
                        method: 'POST',
                        headers: { 'X-Frappe-CSRF-Token': frappe.csrf_token },
                        body: formData
                    }).then(() => {
                        frm.reload_doc();
                    });

                    frappe.show_alert({ message: __('Downloaded ' + target_filename), indicator: 'green' });
                })
                .catch(err => {
                    console.error(err);
                    frappe.msgprint(__('Failed to download Dispatch Tag — check console.'));
                });
        }, __('Create'));
        sync_files_from_quotation(frm);
        sync_shop_items_from_bom(frm, false);
        toggle_order_type_view(frm);

        // 1. Raven Chat Button
        frm.add_custom_button(__('Chat'), function () {
            open_raven_chat(frm);
        });

        // 2. Print & Attach Button
        frm.add_custom_button(__('Print & Attach'), () => {
            let print_format = frm.meta.default_print_format || 'Standard';
            let url = `/api/method/frappe.utils.print_format.download_pdf?doctype=${frm.doctype}&name=${frm.docname}&format=${print_format}&no_letterhead=0`;

            fetch(url, { headers: { 'X-Frappe-CSRF-Token': frappe.csrf_token } })
                .then(res => res.blob())
                .then(blob => {
                    let formData = new FormData();
                    formData.append('file', blob, `${frm.docname}.pdf`);
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
                .then(() => {
                    frappe.show_alert({ message: 'PDF attached', indicator: 'green' });
                    frm.reload_doc();
                    window.open(url, '_blank');
                })
                .catch(err => {
                    console.error(err);
                    frappe.msgprint('Failed to attach PDF - check console for details.');
                });
        }, __('Print'));


        // Standard "Create" buttons (Sales Invoice, Delivery Note) for all submitted orders
        if (frm.doc.docstatus === 1 && frm.doc.status !== 'Closed') {
            if (flt(frm.doc.per_billed) < 100) {
                frm.add_custom_button(__('Sales Invoice'), () => {
                    frappe.model.open_mapped_doc({
                        method: 'erpnext.selling.doctype.sales_order.sales_order.make_sales_invoice',
                        frm: frm
                    });
                }, __('Create'));
            }
            if (flt(frm.doc.per_delivered) < 100) {
                frm.add_custom_button(__('Delivery Note'), () => {
                    frappe.model.open_mapped_doc({
                        method: 'erpnext.selling.doctype.sales_order.sales_order.make_delivery_note',
                        frm: frm
                    });
                }, __('Create'));
            }
        }

        // 3. For Printing & Shop Workflows (3D Print, Miniature Design, Customized 3D, Shopping Cart) on SUBMITTED Sales Orders (docstatus === 1):
        // Show "Invoice Completed Files" and "Deliver Invoiced Files" under "Create"
        let is_file_wf = ['3D Print Service', 'Miniature Design', 'Customized 3D', 'Shopping Cart', 'Display Parts', 'Product Development'].includes(frm.doc.order_type)
            || ((frm.doc.custom_file_details || []).length > 0);

        if (frm.doc.docstatus === 1 && is_file_wf) {
            frm.add_custom_button(__('Invoice Completed Files'), () => {
                create_partial_doc(frm, 'Sales Invoice', 'Completed');
            }, __('Create'));

            frm.add_custom_button(__('Deliver Invoiced Files'), () => {
                create_partial_doc(frm, 'Delivery Note', 'Invoiced');
            }, __('Create'));
        }
    },

    order_type(frm) {
        toggle_order_type_view(frm);
        ensure_custom_deal_name(frm);
        if (frm.doc.order_type === 'Display Parts') {
            sync_sales_order_display_items(frm);
        }
    },
    validate(frm) {
        if (frm.doc.order_type === 'Display Parts') {
            sync_sales_order_display_items(frm);
        }
        (frm.doc.items || []).forEach(row => {
            if (!row.uom) row.uom = 'Nos';
            if (!row.stock_uom) row.stock_uom = row.uom || 'Nos';
            if (!row.conversion_factor || flt(row.conversion_factor) === 0) row.conversion_factor = 1.0;
            if (!row.stock_qty) row.stock_qty = (flt(row.qty) || 1) * (flt(row.conversion_factor) || 1);
        });
    }
});

// Row calculations on custom_file_details (sales order files)
frappe.ui.form.on('sales order files', {
    support_weight(frm, cdt, cdn) {
        let row = locals[cdt][cdn];
        row.total = ((flt(row.weight) || 0) + (flt(row.support_weight) || 0)) * (flt(row.cost_per_gram) || 0);
        frm.refresh_field('custom_file_details');
    },
    weight(frm, cdt, cdn) { calc_so_file_total(frm, cdt, cdn); },
    cost_per_gram(frm, cdt, cdn) { calc_so_file_total(frm, cdt, cdn); },
    quantity(frm, cdt, cdn) { calc_so_file_total(frm, cdt, cdn); }
});

function calc_so_file_total(frm, cdt, cdn) {
    let row = locals[cdt][cdn];
    row.total = (flt(row.weight) || 0) * (flt(row.cost_per_gram) || 0) * (flt(row.quantity) || 1);
    frm.refresh_field('custom_file_details');
}

function toggle_order_type_view(frm) {
    let is_file_wf = ['3D Print Service', 'Miniature Design', 'Customized 3D', 'Shopping Cart', 'Display Parts', 'Product Development'].includes(frm.doc.order_type)
        || ((frm.doc.custom_file_details || []).length > 0);
    frm.toggle_display('custom_file_details', is_file_wf);
}

function sync_files_from_quotation(frm) {
    if (!frm.is_new()) return;
    if (frm.doc.custom_file_details && frm.doc.custom_file_details.length > 0) return;

    let source_quotation = (frm.doc.items || [])
        .map(i => i.prevdoc_docname || i.against_sales_order)
        .filter(Boolean)[0];

    if (!source_quotation && frappe.route_options && frappe.route_options.quotation) {
        source_quotation = frappe.route_options.quotation;
    }
    if (frappe.route_options) {
        delete frappe.route_options.quotation;
        delete frappe.route_options.order_type;
        delete frappe.route_options.custom_deal_name;
    }

    if (source_quotation) {
        frappe.db.get_doc('Quotation', source_quotation).then(quo => {
            if (!quo) return;

            let file_wf_types = ['3D Print Service', 'Miniature Design', 'Customized 3D'];
            let is_file_wf = file_wf_types.includes(quo.order_type)
                || ((quo.custom_deal_name || '').includes('_PS_'))
                || ((quo.custom_deal_name || '').includes('_Min_'))
                || ((quo.custom_deal_name || '').includes('_CD_'));

            if (is_file_wf && frm.doc.order_type !== quo.order_type) {
                frm.set_value('order_type', quo.order_type || '3D Print Service');
            } else if (!is_file_wf && frm.doc.order_type !== 'Sales') {
                frm.set_value('order_type', 'Sales');
            }

            if (quo.custom_deal_name && !frm.doc.custom_deal_name) {
                frm.set_value('custom_deal_name', quo.custom_deal_name);
            }

            if (!is_file_wf || !quo.custom_file_details || quo.custom_file_details.length === 0) return;

            frm.clear_table('custom_file_details');

            (quo.custom_file_details || []).forEach(row => {
                let units = Math.max(1, Math.round(row.quantity || 1));
                let fg_val = row.custom_finished_good || '';
                if (!fg_val) {
                    let mg = (row.material_general || '').toUpperCase();
                    if (mg.includes('PLA')) fg_val = 'FG_FDM_PLA';
                    else if (mg.includes('PETG')) fg_val = 'FG_FDM_PETG';
                    else if (mg.includes('ABS')) fg_val = 'FG_FDM_ABS';
                    else if (mg.includes('RESIN') || mg.includes('SLA')) fg_val = 'FG_SLA_RESIN';
                }
                for (let i = 1; i <= units; i++) {
                    let new_row = frm.add_child('custom_file_details');
                    new_row.file_name = row.file_name;
                    new_row.description = row.description || '';
                    new_row.dimensions = row.dimensions || '';
                    new_row.weight = flt(row.weight) || 0;
                    new_row.support_weight = flt(row.support_weight) || 0;
                    new_row.material_general = row.material_general || '';
                    new_row.material_item = row.material_item || '';
                    new_row.custom_finished_good = fg_val;
                    new_row.cost_per_gram = flt(row.cost_per_gram) || 0;
                    new_row.quantity = 1;
                    new_row.total = ((flt(row.weight) || 0) + (flt(row.support_weight) || 0)) * (flt(row.cost_per_gram) || 0);
                    new_row.status = 'Not Printed';
                    new_row.build = '';
                }
            });

            // Ensure exact bargained rates from Quotation are preserved on Sales Order items
            let is_min_so = (frm.doc.order_type === 'Miniature Design');
            let is_cd_so = (frm.doc.order_type === 'Customized 3D');
            (frm.doc.items || []).forEach(si => {
                let qi = (quo.items || []).find(q => q.item_code === si.item_code);
                if (qi) {
                    si.rate = flt(qi.rate);
                    si.price_list_rate = flt(qi.price_list_rate || qi.rate);
                    si.amount = flt(si.qty) * flt(si.rate);
                } else if (is_min_so || is_cd_so) {
                    let code = (si.item_code || '').toLowerCase();
                    if (!code.includes('miniature') && !code.includes('sculpt') && !code.includes('custom') && !code.includes('gift') && !code.includes('keychain') && !code.includes('toy')) {
                        si.rate = 0;
                        si.price_list_rate = 0;
                        si.amount = 0;
                    }
                }
            });

            let file_rows = frm.doc.custom_file_details || [];
            frm.toggle_display('custom_file_details', true);
            frm.refresh_field('custom_file_details');
            frm.refresh_field('items');

            frappe.show_alert({
                message: __('Carried {0} part(s) from Quotation into File Details build table', [file_rows.length]),
                indicator: 'green'
            });
        });
    }
}

function create_partial_doc(frm, doc_type, filter_status) {
    let ready_rows = (frm.doc.custom_file_details || []).filter(f => f.status === filter_status);

    if (!ready_rows.length) {
        frappe.msgprint(__('No files with status "{0}" ready for {1} yet.', [filter_status, doc_type]));
        return;
    }

    let used_so_items = new Set();
    let items_for_doc = [];

    // For Miniature Design and Customized 3D: include any unbilled design/custom product items in the Sales Invoice
    let is_design_or_custom = ['Miniature Design', 'Customized 3D'].includes(frm.doc.order_type);
    if (doc_type === 'Sales Invoice' && is_design_or_custom) {
        (frm.doc.items || []).forEach(i => {
            if (flt(i.amount) > 0 && flt(i.billed_amt) < flt(i.amount)) {
                items_for_doc.push({
                    item_code: i.item_code,
                    item_name: i.item_name,
                    description: i.description,
                    qty: i.qty || 1,
                    rate: i.rate || 0,
                    price_list_rate: i.price_list_rate || i.rate || 0,
                    amount: (i.qty || 1) * (i.rate || 0),
                    sales_order: frm.doc.name,
                    so_detail: i.name
                });
                used_so_items.add(i.name);
            }
        });
    }

    ready_rows.forEach(f => {
        let f_idx = (frm.doc.custom_file_details || []).findIndex(row => String(row.name) === String(f.name));

        function is_eligible(i) {
            if (!i || used_so_items.has(i.name)) return false;
            if (doc_type === 'Sales Invoice') {
                return (flt(i.amount) <= 0) || (flt(i.billed_amt) < flt(i.amount));
            } else if (doc_type === 'Delivery Note') {
                return (flt(i.qty) <= 0) || (flt(i.delivered_qty) < flt(i.qty));
            }
            return true;
        }

        let fg_code = f.custom_finished_good || '';
        if (!fg_code) {
            let mg = (f.material_general || '').toUpperCase();
            if (mg.includes('PLA')) fg_code = 'FG_FDM_PLA';
            else if (mg.includes('PETG')) fg_code = 'FG_FDM_PETG';
            else if (mg.includes('ABS')) fg_code = 'FG_FDM_ABS';
            else if (mg.includes('RESIN') || mg.includes('SLA')) fg_code = 'FG_SLA_RESIN';
        }

        let matching_so_item = null;

        // 1. Try matching by row index if eligible and material/FG matches
        if (f_idx >= 0 && frm.doc.items[f_idx] && is_eligible(frm.doc.items[f_idx])) {
            let candidate = frm.doc.items[f_idx];
            if (candidate.item_code === f.material_item || (fg_code && candidate.item_code === fg_code)) {
                matching_so_item = candidate;
            }
        }

        // 2. Try matching by custom_file_row_link if eligible and material/FG matches
        if (!matching_so_item) {
            matching_so_item = (frm.doc.items || []).find(i => is_eligible(i) && (i.item_code === f.material_item || (fg_code && i.item_code === fg_code)) && String(i.custom_file_row_link) === String(f.name));
        }

        // 3. Fallback to first eligible unused so_item matching material_item or fg_code
        if (!matching_so_item) {
            matching_so_item = (frm.doc.items || []).find(i => is_eligible(i) && (i.item_code === f.material_item || (fg_code && i.item_code === fg_code)));
        }

        if (matching_so_item) {
            used_so_items.add(matching_so_item.name);
        }

        let target_item_code = matching_so_item ? matching_so_item.item_code : (fg_code || f.material_item);
        let rate = matching_so_item ? flt(matching_so_item.rate) : (flt(f.cost_per_gram) || 0);
        let qty = (matching_so_item && matching_so_item.stock_uom === 'Nos') ? (flt(f.quantity) || 1) : (matching_so_item ? flt(matching_so_item.qty) : (flt(f.weight) || 1));
        let amount = matching_so_item ? (qty * rate) : (qty * rate);

        let wh = (matching_so_item && matching_so_item.warehouse) || 'Finished Goods - PVlabs';
        if (frm.doc.order_type === 'Display Parts') {
            wh = 'DisplayParts - PVlabs';
        } else if (frm.doc.order_type === 'Product Development') {
            wh = 'Testing - PVlabs';
        } else if (doc_type === 'Delivery Note' && !wh.includes('Finished Goods')) {
            wh = 'Finished Goods - PVlabs';
        }

        let item_row = {
            item_code: target_item_code,
            item_name: matching_so_item ? (matching_so_item.item_name || target_item_code) : target_item_code,
            description: `${f.file_name} (${f.material_general || f.material_item || '3D Print'}) - ${f.weight || 0}g`,
            qty: qty,
            rate: rate,
            price_list_rate: rate,
            amount: amount,
            warehouse: wh,
            custom_file_row_link: String(f.name)
        };

        if (matching_so_item) {
            if (doc_type === 'Sales Invoice') {
                item_row.sales_order = frm.doc.name;
                item_row.so_detail = matching_so_item.name;
            } else if (doc_type === 'Delivery Note') {
                item_row.against_sales_order = frm.doc.name;
                item_row.so_detail = matching_so_item.name;
            }
        }
        items_for_doc.push(item_row);
    });

    let new_doc = {
        doctype: doc_type,
        customer: frm.doc.customer,
        custom_deal_name: frm.doc.custom_deal_name || '',
        items: items_for_doc
    };

    let consolidated_map = {};
    ready_rows.forEach(f => {
        let fg_code = f.custom_finished_good || '';
        let key = `${f.file_name}:::${f.material_item || fg_code || ''}:::${flt(f.cost_per_gram) || 0}`;
        if (!consolidated_map[key]) {
            consolidated_map[key] = {
                file_name: f.file_name,
                description: f.description || '',
                weight: flt(f.weight) || 0,
                material_general: f.material_general || '',
                material_item: f.material_item || '',
                custom_finished_good: fg_code,
                cost_per_gram: flt(f.cost_per_gram) || 0,
                quantity: 0,
                total: 0
            };
        }
        let units = Math.max(1, parseInt(f.quantity) || 1);
        consolidated_map[key].quantity += units;
        consolidated_map[key].total += (flt(f.weight) || 0) * (flt(f.cost_per_gram) || 0) * units;
    });

    // Always attach file details to both Delivery Note and Sales Invoice if files exist
    if (Object.keys(consolidated_map).length > 0) {
        new_doc.custom_file_detail = Object.values(consolidated_map);
    }

    if (doc_type === 'Sales Invoice') {
        new_doc.sales_order = frm.doc.name;
    }

    frappe.call({
        method: 'frappe.client.insert',
        args: { doc: new_doc },
        freeze: true,
        freeze_message: __('Creating {0}...', [doc_type]),
        callback: function(r) {
            if (!r.message) return;
            let created_doc = r.message;
            let next_status = (doc_type === 'Sales Invoice') ? 'Invoiced' : 'Delivered';
            let ready_names = new Set(ready_rows.map(x => x.name));

            (frm.doc.custom_file_details || []).forEach(row => {
                if (ready_names.has(row.name)) {
                    row.status = next_status;
                }
            });
            frm.refresh_field('custom_file_details');

            frm.save('Update').then(() => {
                frappe.show_alert({
                    message: __('{0} {1} created! {2} file(s) marked as {3}', [doc_type, created_doc.name, ready_rows.length, next_status]),
                    indicator: 'green'
                });
                frappe.set_route('Form', doc_type, created_doc.name);
            }).catch(err => {
                console.warn('Sales order update notice:', err);
                frappe.set_route('Form', doc_type, created_doc.name);
            });
        }
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

function ensure_custom_deal_name(frm) {
    if (frm.doc.order_type === 'Display Parts') {
        if (!frm.doc.custom_deal_name || !frm.doc.custom_deal_name.startsWith('DP_')) {
            frappe.db.get_list('Sales Order', {
                filters: [['custom_deal_name', 'like', 'DP%']],
                fields: ['custom_deal_name'],
                limit_page_length: 500
            }).then(res => {
                let max_num = 0;
                (res || []).forEach(r => {
                    let val = (r.custom_deal_name || '').trim();
                    let match = val.match(/^DP_?(\d+)/i);
                    if (match && match[1]) {
                        let num = parseInt(match[1], 10);
                        if (!isNaN(num) && num > max_num) {
                            max_num = num;
                        }
                    }
                });
                let next_num = max_num + 1;
                let pad = String(next_num).padStart(3, '0');
                frm.set_value('custom_deal_name', 'DP_' + pad);
            });
        }
        return;
    }

    if (!frm.doc.custom_deal_name) {
        let cust = (frm.doc.customer_name || frm.doc.customer || 'Customer').trim().replace(/[^a-zA-Z0-9]/g, '');
        let d = (frm.doc.transaction_date || frappe.datetime.nowdate()).replace(/-/g, '').slice(2);
        let prefix = 'SO';
        if (frm.doc.order_type === 'Shopping Cart') prefix = 'SHOP';
        else if (frm.doc.order_type === 'Product Development') prefix = 'NPD';
        else if (frm.doc.order_type === '3D Print Service') prefix = 'PS';
        else if (frm.doc.order_type === 'Miniature Design') prefix = 'Min';
        else if (frm.doc.order_type === 'Customized 3D') prefix = 'CD';
        let deal = `${cust}_${prefix}_${d}`;
        frm.set_value('custom_deal_name', deal);
    }
}


// ============================================================================
// Shop Order BOM to Build Queue Unpacker
// ============================================================================
function unpack_bom_items_interactive(frm) {
    if (!frm.doc.items || frm.doc.items.length === 0) {
        frappe.msgprint(__('No items found in this Sales Order to unpack.'));
        return;
    }

    if (frm.doc.custom_file_details && frm.doc.custom_file_details.length > 0) {
        frappe.confirm(
            __('The File Details table already contains {0} row(s). Re-unpacking will clear unprinted rows and regenerate from active BOMs. Proceed?', [frm.doc.custom_file_details.length]),
            () => {
                unpack_bom_items(frm, true);
            }
        );
    } else {
        unpack_bom_items(frm, true);
    }
}

function sync_shop_items_from_bom(frm, is_manual) {
    try {
        if (!is_manual) {
            if (frm.doc.docstatus !== 0) return;
            if (frm.doc.custom_file_details && frm.doc.custom_file_details.length > 0) return;
            if (!frm.doc.items || frm.doc.items.length === 0) return;

            let source_quotation = (frm.doc.items || [])
                .map(i => i.prevdoc_docname || i.against_sales_order)
                .filter(Boolean)[0];
            if (source_quotation && ['3D Print Service', 'Miniature Design', 'Customized 3D'].includes(frm.doc.order_type)) {
                return;
            }
        }

        unpack_bom_items(frm, is_manual);
    } catch (e) {
        console.warn('sync_shop_items_from_bom warning:', e);
    }
}

function unpack_bom_items(frm, show_alerts) {
    let items = frm.doc.items || [];
    if (!items.length) return;

    let bom_promises = items.map(item => {
        let code = item.item_code;
        return frappe.db.get_list('BOM', {
            filters: { item: code, is_active: 1, docstatus: 1 },
            fields: ['name', 'is_default'],
            limit: 5
        }).then(boms => {
            if (!boms || !boms.length) return null;
            let target_bom = boms.find(b => b.is_default) || boms[0];
            return frappe.db.get_doc('BOM', target_bom.name).then(bom_doc => {
                return {
                    item: item,
                    bom: bom_doc
                };
            });
        }).catch(err => {
            console.warn('BOM lookup error:', err);
            return null;
        });
    });

    Promise.all(bom_promises).then(results => {
        let valid_entries = results.filter(Boolean);
        if (!valid_entries.length) {
            if (show_alerts) {
                frappe.msgprint(__('No active Bill of Materials (BOM) found for the ordered items. Please ensure BOM is submitted for each variant.'));
            }
            return;
        }

        // If manual re-unpack, keep any rows that are already in Build, Completed, or Invoiced
        if (show_alerts && frm.doc.custom_file_details && frm.doc.custom_file_details.length > 0) {
            let in_progress_rows = frm.doc.custom_file_details.filter(r => r.status && r.status !== 'Not Printed');
            frm.clear_table('custom_file_details');
            in_progress_rows.forEach(r => {
                let row = frm.add_child('custom_file_details');
                Object.assign(row, r);
            });
        }

        let added_count = 0;
        valid_entries.forEach(entry => {
            let item = entry.item;
            let bom = entry.bom;
            let units = Math.max(1, Math.round(flt(item.qty) || 1));
            let bom_items = bom.items || [];

            bom_items.forEach(bom_item => {
                let raw_code = bom_item.item_code;
                let raw_weight = flt(bom_item.qty) || 0;
                let raw_rate = flt(bom_item.rate) || 0;
                let code_upper = (raw_code || '').toUpperCase();
                let mat_gen = 'PLA';
                if (code_upper.includes('PETG')) mat_gen = 'PETG';
                else if (code_upper.includes('ABS')) mat_gen = 'ABS';
                else if (code_upper.includes('RESIN') || code_upper.includes('SLA')) mat_gen = 'Resin';
                else if (code_upper.includes('TPU')) mat_gen = 'TPU';

                for (let u = 1; u <= units; u++) {
                    let new_row = frm.add_child('custom_file_details');
                    new_row.file_name = units > 1 ? (item.item_name || item.item_code) + ' (Unit ' + u + ')' : (item.item_name || item.item_code);
                    new_row.description = item.description || item.item_name || item.item_code;
                    new_row.weight = raw_weight;
                    new_row.material_general = mat_gen;
                    new_row.material_item = raw_code;
                    new_row.custom_finished_good = item.item_code;
                    new_row.cost_per_gram = raw_rate;
                    new_row.quantity = 1;
                    new_row.total = raw_weight * raw_rate;
                    new_row.status = 'Not Printed';
                    new_row.build = '';
                    added_count++;
                }
            });
        });

        if (added_count > 0) {
            frm.refresh_field('custom_file_details');
            frm.toggle_display('custom_file_details', true);
            if (show_alerts) {
                frappe.show_alert({
                    message: __('Unpacked ' + added_count + ' printable unit(s) from BOM into File Details build queue.'),
                    indicator: 'green'
                });
            }
        }
    }).catch(err => {
        console.warn('unpack_bom_items error:', err);
    });
}


frappe.ui.form.on('sales order files', {
    material_item(frm, cdt, cdn) {
        set_display_part_file_defaults(frm, cdt, cdn);
        sync_sales_order_display_items(frm);
    },
    material_general(frm, cdt, cdn) {
        set_display_part_file_defaults(frm, cdt, cdn);
        sync_sales_order_display_items(frm);
    },
    weight(frm, cdt, cdn) {
        calc_so_file_total(frm, cdt, cdn);
        sync_sales_order_display_items(frm);
    },
    quantity(frm, cdt, cdn) {
        calc_so_file_total(frm, cdt, cdn);
        sync_sales_order_display_items(frm);
    },
    custom_file_details_remove(frm) {
        sync_sales_order_display_items(frm);
    }
});

function get_display_fg_for_material(mat) {
    let str = (mat || '').toUpperCase();
    if (str.includes('PETG')) return 'DISPLAY_PETG';
    if (str.includes('ABS')) return 'DISPLAY_ABS';
    if (str.includes('RESIN') || str.includes('SLA')) return 'DISPLAY_RESIN';
    if (str.includes('TPU')) return 'DISPLAY_TPU';
    return 'DISPLAY_PLA';
}

function set_display_part_file_defaults(frm, cdt, cdn) {
    let row = locals[cdt][cdn];
    if (frm.doc.order_type === 'Display Parts') {
        row.cost_per_gram = 0;
        row.total = 0;
        row.custom_finished_good = get_display_fg_for_material((row.material_general || '') + ' ' + (row.material_item || ''));
        frm.refresh_field('custom_file_details');
    }
}

function sync_sales_order_display_items(frm) {
    if (frm.doc.order_type !== 'Display Parts') return;
    
    let file_rows = frm.doc.custom_file_details || [];
    if (!file_rows.length) return;

    let target_wh = 'DisplayParts - PVlabs';
    let delivery_d = frm.doc.delivery_date || frappe.datetime.nowdate();

    // Group quantities by display FG code
    let fg_groups = {};
    file_rows.forEach(f => {
        let fg_code = f.custom_finished_good;
        if (!fg_code) {
            fg_code = get_display_fg_for_material((f.material_general || '') + ' ' + (f.material_item || ''));
            f.custom_finished_good = fg_code;
        }
        let qty = Math.max(1, parseInt(f.quantity) || 1);
        let desc = f.file_name ? `${f.file_name} (${f.material_general || 'PLA'})` : fg_code;
        
        if (!fg_groups[fg_code]) {
            fg_groups[fg_code] = {
                item_code: fg_code,
                item_name: fg_code,
                qty: 0,
                rate: 0,
                amount: 0,
                warehouse: target_wh,
                delivery_date: delivery_d,
                descriptions: []
            };
        }
        fg_groups[fg_code].qty += qty;
        if (f.file_name) fg_groups[fg_code].descriptions.push(desc);
    });

    // Clear and rebuild items table
    frm.clear_table('items');
    Object.keys(fg_groups).forEach(code => {
        let g = fg_groups[code];
        let child = frm.add_child('items');
        child.item_code = g.item_code;
        child.item_name = g.item_name;
        child.qty = g.qty;
        child.stock_qty = g.qty;
        child.uom = 'Nos';
        child.stock_uom = 'Nos';
        child.conversion_factor = 1.0;
        child.rate = 0;
        child.amount = 0;
        child.base_rate = 0;
        child.base_amount = 0;
        child.warehouse = g.warehouse;
        child.delivery_date = g.delivery_date;
        child.description = g.descriptions.length ? g.descriptions.join(', ') : `Display Part (${g.item_code})`;
    });

    frm.refresh_field('items');
    frm.refresh_field('custom_file_details');
}


function sync_google_drive_link_from_source(frm) {
    if (!frm.is_new() || frm.doc.custom_google_drive_link) return;

    let source_quotation = (frm.doc.items || [])
        .map(i => i.prevdoc_docname || i.against_sales_order)
        .filter(Boolean)[0];

    if (!source_quotation && frappe.route_options && frappe.route_options.quotation) {
        source_quotation = frappe.route_options.quotation;
    }

    if (source_quotation) {
        frappe.db.get_value('Quotation', source_quotation, ['custom_google_drive_link', 'opportunity'], (res) => {
            if (res && res.custom_google_drive_link) {
                frm.set_value('custom_google_drive_link', res.custom_google_drive_link);
            } else if (res && res.opportunity) {
                frappe.db.get_value('Opportunity', res.opportunity, 'custom_google_drive_link', (opp_res) => {
                    if (opp_res && opp_res.custom_google_drive_link) {
                        frm.set_value('custom_google_drive_link', opp_res.custom_google_drive_link);
                    }
                });
            }
        });
    } else if (frm.doc.customer) {
        frappe.db.get_value('Customer', frm.doc.customer, 'lead_name', (c_res) => {
            if (c_res && c_res.lead_name) {
                frappe.db.get_value('Lead', c_res.lead_name, 'custom_google_drive_link', (l_res) => {
                    if (l_res && l_res.custom_google_drive_link) {
                        frm.set_value('custom_google_drive_link', l_res.custom_google_drive_link);
                    }
                });
            }
        });
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
