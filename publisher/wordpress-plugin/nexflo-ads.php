<?php
/**
 * Plugin Name:       Nexflo Ad Revenue Booster
 * Plugin URI:        https://nexflo.ai/publishers
 * Description:       Earn more from your website ads with header bidding. One-click setup — no ad-tech expertise required.
 * Version:           1.0.0
 * Author:            Nexflo
 * Author URI:        https://nexflo.ai
 * License:           GPL v2 or later
 * License URI:       https://www.gnu.org/licenses/gpl-2.0.html
 * Text Domain:       nexflo-ads
 * Requires at least: 5.0
 * Requires PHP:      7.4
 * Tested up to:      6.7
 */

if (!defined('ABSPATH')) {
    exit;
}

define('NEXFLO_ADS_VERSION', '1.0.0');
define('NEXFLO_ADS_PLUGIN_DIR', plugin_dir_path(__FILE__));
define('NEXFLO_ADS_PLUGIN_URL', plugin_dir_url(__FILE__));

/**
 * Register settings on activation
 */
function nexflo_ads_activate() {
    add_option('nexflo_site_id', '');
    add_option('nexflo_auto_place', '1');
    add_option('nexflo_ad_sizes', '');
    add_option('nexflo_floor_price', '');
    add_option('nexflo_timeout', '1500');
    add_option('nexflo_enabled', '1');
}
register_activation_hook(__FILE__, 'nexflo_ads_activate');

/**
 * Clean up on uninstall
 */
function nexflo_ads_uninstall() {
    delete_option('nexflo_site_id');
    delete_option('nexflo_auto_place');
    delete_option('nexflo_ad_sizes');
    delete_option('nexflo_floor_price');
    delete_option('nexflo_timeout');
    delete_option('nexflo_enabled');
}
register_uninstall_hook(__FILE__, 'nexflo_ads_uninstall');

/**
 * Inject the Prebid wrapper into <head>
 */
function nexflo_ads_inject_script() {
    if (is_admin()) return;
    if (get_option('nexflo_enabled') !== '1') return;

    $site_id = sanitize_text_field(get_option('nexflo_site_id'));
    if (empty($site_id)) return;

    $attrs = 'data-site-id="' . esc_attr($site_id) . '"';

    $auto_place = get_option('nexflo_auto_place');
    if ($auto_place !== '1') {
        $attrs .= ' data-auto-place="false"';
    }

    $ad_sizes = sanitize_text_field(get_option('nexflo_ad_sizes'));
    if (!empty($ad_sizes)) {
        $attrs .= ' data-ad-sizes="' . esc_attr($ad_sizes) . '"';
    }

    $floor = sanitize_text_field(get_option('nexflo_floor_price'));
    if (!empty($floor) && is_numeric($floor)) {
        $attrs .= ' data-floor="' . esc_attr($floor) . '"';
    }

    $timeout = sanitize_text_field(get_option('nexflo_timeout'));
    if (!empty($timeout) && is_numeric($timeout)) {
        $attrs .= ' data-timeout="' . esc_attr($timeout) . '"';
    }

    echo '<script src="https://cdn.nexflo.ai/prebid-wrapper.js" ' . $attrs . ' async></script>' . "\n";
}
add_action('wp_head', 'nexflo_ads_inject_script', 1);

/**
 * Admin menu
 */
function nexflo_ads_admin_menu() {
    add_options_page(
        'Nexflo Ads',
        'Nexflo Ads',
        'manage_options',
        'nexflo-ads',
        'nexflo_ads_settings_page'
    );
}
add_action('admin_menu', 'nexflo_ads_admin_menu');

/**
 * Register settings
 */
function nexflo_ads_register_settings() {
    register_setting('nexflo_ads_settings', 'nexflo_site_id', [
        'sanitize_callback' => 'sanitize_text_field',
    ]);
    register_setting('nexflo_ads_settings', 'nexflo_auto_place', [
        'sanitize_callback' => 'sanitize_text_field',
    ]);
    register_setting('nexflo_ads_settings', 'nexflo_ad_sizes', [
        'sanitize_callback' => 'sanitize_text_field',
    ]);
    register_setting('nexflo_ads_settings', 'nexflo_floor_price', [
        'sanitize_callback' => 'sanitize_text_field',
    ]);
    register_setting('nexflo_ads_settings', 'nexflo_timeout', [
        'sanitize_callback' => 'sanitize_text_field',
    ]);
    register_setting('nexflo_ads_settings', 'nexflo_enabled', [
        'sanitize_callback' => 'sanitize_text_field',
    ]);
}
add_action('admin_init', 'nexflo_ads_register_settings');

/**
 * Settings page
 */
function nexflo_ads_settings_page() {
    if (!current_user_can('manage_options')) return;

    $site_id    = get_option('nexflo_site_id', '');
    $auto_place = get_option('nexflo_auto_place', '1');
    $ad_sizes   = get_option('nexflo_ad_sizes', '');
    $floor      = get_option('nexflo_floor_price', '');
    $timeout    = get_option('nexflo_timeout', '1500');
    $enabled    = get_option('nexflo_enabled', '1');
    ?>
    <div class="wrap">
        <h1>Nexflo Ad Revenue Booster</h1>
        <p>Earn more from your website ads with header bidding technology. No ad-tech expertise required.</p>

        <?php if (empty($site_id)) : ?>
        <div class="notice notice-warning">
            <p><strong>Getting started:</strong> Enter your Site ID below. Don't have one? <a href="https://nexflo.ai/publishers/signup" target="_blank">Sign up free</a></p>
        </div>
        <?php elseif ($enabled === '1') : ?>
        <div class="notice notice-success">
            <p>Nexflo Ads is <strong>active</strong> and earning you money.</p>
        </div>
        <?php endif; ?>

        <form method="post" action="options.php">
            <?php settings_fields('nexflo_ads_settings'); ?>

            <table class="form-table" role="presentation">
                <tr>
                    <th scope="row"><label for="nexflo_enabled">Enable Ads</label></th>
                    <td>
                        <label>
                            <input type="checkbox" id="nexflo_enabled" name="nexflo_enabled" value="1" <?php checked($enabled, '1'); ?>>
                            Show ads on my site
                        </label>
                    </td>
                </tr>
                <tr>
                    <th scope="row"><label for="nexflo_site_id">Site ID</label></th>
                    <td>
                        <input type="text" id="nexflo_site_id" name="nexflo_site_id"
                               value="<?php echo esc_attr($site_id); ?>"
                               class="regular-text" placeholder="e.g. site_abc123">
                        <p class="description">Your unique publisher ID from <a href="https://nexflo.ai/publishers" target="_blank">nexflo.ai</a></p>
                    </td>
                </tr>
                <tr>
                    <th scope="row"><label for="nexflo_auto_place">Auto-Place Ads</label></th>
                    <td>
                        <label>
                            <input type="checkbox" id="nexflo_auto_place" name="nexflo_auto_place" value="1" <?php checked($auto_place, '1'); ?>>
                            Automatically place ads in optimal positions
                        </label>
                        <p class="description">When enabled, ads are placed at the top of content, mid-article, and in the sidebar. Disable to use manual placement with shortcodes.</p>
                    </td>
                </tr>
                <tr>
                    <th scope="row"><label for="nexflo_ad_sizes">Ad Sizes</label></th>
                    <td>
                        <input type="text" id="nexflo_ad_sizes" name="nexflo_ad_sizes"
                               value="<?php echo esc_attr($ad_sizes); ?>"
                               class="regular-text" placeholder="e.g. 728x90,300x250">
                        <p class="description">Optional. Comma-separated sizes. Leave blank for automatic sizing.</p>
                    </td>
                </tr>
                <tr>
                    <th scope="row"><label for="nexflo_floor_price">Floor Price ($)</label></th>
                    <td>
                        <input type="text" id="nexflo_floor_price" name="nexflo_floor_price"
                               value="<?php echo esc_attr($floor); ?>"
                               class="small-text" placeholder="0.50">
                        <p class="description">Optional. Minimum CPM in USD. Ads below this price won't show.</p>
                    </td>
                </tr>
                <tr>
                    <th scope="row"><label for="nexflo_timeout">Bid Timeout (ms)</label></th>
                    <td>
                        <input type="number" id="nexflo_timeout" name="nexflo_timeout"
                               value="<?php echo esc_attr($timeout); ?>"
                               class="small-text" min="500" max="5000" step="100">
                        <p class="description">How long to wait for bids before showing the page. Default: 1500ms.</p>
                    </td>
                </tr>
            </table>

            <?php submit_button('Save Settings'); ?>
        </form>

        <?php if (!$auto_place || $auto_place !== '1') : ?>
        <hr>
        <h2>Manual Placement</h2>
        <p>Use these shortcodes to place ads manually in your posts and pages:</p>
        <table class="widefat" style="max-width:600px;">
            <thead><tr><th>Shortcode</th><th>Description</th></tr></thead>
            <tbody>
                <tr><td><code>[nexflo_ad]</code></td><td>Default ad (300x250)</td></tr>
                <tr><td><code>[nexflo_ad size="728x90"]</code></td><td>Leaderboard</td></tr>
                <tr><td><code>[nexflo_ad size="320x50"]</code></td><td>Mobile banner</td></tr>
            </tbody>
        </table>
        <?php endif; ?>
    </div>
    <?php
}

/**
 * Shortcode for manual ad placement: [nexflo_ad size="300x250"]
 */
function nexflo_ads_shortcode($atts) {
    if (get_option('nexflo_enabled') !== '1') return '';
    if (empty(get_option('nexflo_site_id'))) return '';

    $atts = shortcode_atts(['size' => '300x250'], $atts, 'nexflo_ad');
    $sizes = explode('x', sanitize_text_field($atts['size']));

    if (count($sizes) !== 2) return '';

    $width = intval($sizes[0]);
    $height = intval($sizes[1]);
    $slot_id = 'nexflo-shortcode-' . wp_unique_id();

    return sprintf(
        '<div id="%s" data-nexflo-ad data-nexflo-sizes="%dx%d" style="text-align:center;margin:16px auto;min-height:%dpx;max-width:%dpx;"></div>',
        esc_attr($slot_id),
        $width,
        $height,
        $height,
        $width
    );
}
add_shortcode('nexflo_ad', 'nexflo_ads_shortcode');

/**
 * Add settings link on plugins page
 */
function nexflo_ads_settings_link($links) {
    $settings_link = '<a href="options-general.php?page=nexflo-ads">Settings</a>';
    array_unshift($links, $settings_link);
    return $links;
}
add_filter('plugin_action_links_' . plugin_basename(__FILE__), 'nexflo_ads_settings_link');
