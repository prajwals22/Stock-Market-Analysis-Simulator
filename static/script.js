document.getElementById("getPriceBtn").addEventListener("click", async () => {
    const stockName = document.getElementById("stockInput").value.trim();
    const outputBox = document.getElementById("output");
    outputBox.textContent = "Fetching price...";

    if (!stockName) {
        outputBox.textContent = "❌ Please enter a stock name!";
        return;
    }

    try {
        const response = await fetch("/get-price", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ stock_name: stockName })
        });
        const data = await response.json();
        outputBox.textContent = data.price;
    } catch (err) {
        outputBox.textContent = "❌ Error: " + err.message;
    }
});
