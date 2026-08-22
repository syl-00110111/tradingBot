use anyhow::Result;
use clap::Parser;
use tracing::{info, Level};
use tracing_subscriber::FmtSubscriber;

use botv5::config::{Config, RunMode};
use botv5::engine::TradingEngine;

#[derive(Parser, Debug)]
#[command(author, version, about = "Botv5 Rust Cryptocurrencies Trading Engine", long_about = None)]
struct Args {
    #[arg(short, long, value_enum, default_value_t = RunMode::Live)]
    mode: RunMode,
}

#[tokio::main]
async fn main() -> Result<()> {
    let args = Args::parse();

    let subscriber = FmtSubscriber::builder()
        .with_max_level(Level::INFO)
        .finish();
    tracing::subscriber::set_global_default(subscriber)
        .expect("setting default subscriber failed");

    info!("Initializing Botv5 Rust skeleton in mode: {:?}", args.mode);

    let config = Config::load_and_merge(args.mode)?;
    info!("Configuration loaded. Exchange ID: {}", config.exchange_id);

    let mut engine = TradingEngine::new(config);
    info!("Starting trading engine loop...");
    engine.run().await?;

    Ok(())
}
